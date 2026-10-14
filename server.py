"""Gold Engine Fleet: local FastAPI + SQLite PWA prototype.
Not designed to be exposed on the public Internet without authentication and HTTPS.
"""
from __future__ import annotations

import csv
import base64
from contextlib import contextmanager
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DB = Path(os.environ.get('GEFLEET_DB', str(DATA / 'fleet.db'))).resolve()
TEMPLATE = json.loads((ROOT / 'checklist_liviano.json').read_text(encoding='utf-8'))
CHECK_IDS = {f'{gi}-{ii}' for gi, g in enumerate(TEMPLATE) for ii, _ in enumerate(g['items'])}
SCHEMA_VERSION = 1
MAX_ODOMETER_KM = 10_000_000
MAX_SERVICE_INTERVAL_KM = 1_000_000
MAX_EXPECTED_ODOMETER_KM = 9_007_199_254_740_991
ODOMETER_CORRECTION_SERVICE = 'Corrección de kilometraje'

app = FastAPI(title='Gold Engine Fleet', docs_url='/api/docs', redoc_url=None)
app.mount('/static', StaticFiles(directory=str(ROOT / 'static')), name='static')


@contextmanager
def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    cn = sqlite3.connect(str(DB), timeout=15, isolation_level=None)
    cn.row_factory = sqlite3.Row
    cn.execute('PRAGMA foreign_keys=ON')
    cn.execute('PRAGMA busy_timeout=15000')
    cn.execute('PRAGMA journal_mode=WAL')
    try:
        yield cn
        cn.commit()
    except Exception:
        cn.rollback()
        raise
    finally:
        cn.close()


def init_db():
    with connect() as cn:
        if cn.execute('PRAGMA user_version').fetchone()[0] > SCHEMA_VERSION:
            raise RuntimeError('La base de datos utiliza una versión de esquema más reciente que esta aplicación.')
        cn.executescript('''
CREATE TABLE IF NOT EXISTS vehicles (
 id INTEGER PRIMARY KEY, plate TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
 vehicle_type TEXT NOT NULL DEFAULT 'POR_VERIFICAR',
 model TEXT NOT NULL DEFAULT '', brand TEXT NOT NULL DEFAULT '',
 odometer INTEGER NOT NULL DEFAULT 0 CHECK(odometer >= 0),
 notes TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS inspections (
 id INTEGER PRIMARY KEY, client_uuid TEXT UNIQUE NOT NULL, vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
 date TEXT NOT NULL, inspector TEXT NOT NULL, odometer INTEGER NOT NULL CHECK(odometer >= 0),
 answers_json TEXT NOT NULL, observations TEXT NOT NULL DEFAULT '', photos_json TEXT NOT NULL DEFAULT '[]',
 failure_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS parts (
 id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
 part_number TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'Otros',
 unit TEXT NOT NULL DEFAULT 'unidad', min_milli INTEGER NOT NULL DEFAULT 0 CHECK(min_milli >= 0),
 notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS compatibility (
 vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),part_id INTEGER NOT NULL REFERENCES parts(id),
 notes TEXT NOT NULL DEFAULT '', PRIMARY KEY(vehicle_id,part_id)
);
CREATE TABLE IF NOT EXISTS movements (
 id INTEGER PRIMARY KEY, part_id INTEGER NOT NULL REFERENCES parts(id),
 movement_type TEXT NOT NULL CHECK(movement_type IN ('ENTRADA','SALIDA')),
 qty_milli INTEGER NOT NULL CHECK(qty_milli > 0),
 vehicle_id INTEGER REFERENCES vehicles(id), reference TEXT NOT NULL DEFAULT '',
 responsible TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mov_part ON movements(part_id,id);
CREATE TABLE IF NOT EXISTS maintenance (
 id INTEGER PRIMARY KEY, vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
 service_name TEXT NOT NULL, performed_on TEXT NOT NULL, odometer INTEGER NOT NULL CHECK(odometer >= 0),
 interval_km INTEGER CHECK(interval_km IS NULL OR interval_km > 0),
 next_due_km INTEGER, notes TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS requests (
 id INTEGER PRIMARY KEY, vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
 requester TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'PENDIENTE',
 approved_by TEXT NOT NULL DEFAULT '', received_by TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS request_lines (
 id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL REFERENCES requests(id),
 part_id INTEGER NOT NULL REFERENCES parts(id), qty_milli INTEGER NOT NULL CHECK(qty_milli > 0),
 delivered_milli INTEGER NOT NULL DEFAULT 0 CHECK(delivered_milli >= 0 AND delivered_milli <= qty_milli)
);
''')
        # executescript commits implicitly. Start the additive migration separately
        # so the table and its schema version are either both committed or neither.
        cn.execute('BEGIN IMMEDIATE')
        try:
            if cn.execute('PRAGMA user_version').fetchone()[0] < 1:
                cn.execute('''CREATE TABLE inventory_operations (
                    operation_key TEXT PRIMARY KEY NOT NULL CHECK(length(operation_key) BETWEEN 1 AND 128),
                    scope TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )''')
                cn.execute('PRAGMA user_version=1')
            cn.execute('COMMIT')
        except Exception:
            cn.execute('ROLLBACK')
            raise


@app.on_event('startup')
def _startup():
    init_db()


def rows(query, args=()):
    with connect() as cn:
        return [dict(r) for r in cn.execute(query, args).fetchall()]


def row(query, args=()):
    with connect() as cn:
        res = cn.execute(query, args).fetchone()
        return dict(res) if res else None


def required(s, label, max_len=250):
    value = str(s or '').strip()
    if not value:
        raise HTTPException(422, f'{label} es obligatorio.')
    if len(value) > max_len:
        raise HTTPException(422, f'{label} demasiado largo.')
    return value


def milli(value: Any) -> int:
    try:
        d = Decimal(str(value))
        if not d.is_finite() or d <= 0 or (d * 1000) != (d * 1000).to_integral_value():
            raise ValueError()
        n = int(d * 1000)
        if n > 2_000_000_000_000:
            raise ValueError()
        return n
    except (ValueError, InvalidOperation, TypeError):
        raise HTTPException(422, 'Cantidad inválida: usa un número positivo con máximo tres decimales.')


def stock(cn, part_id):
    return cn.execute('''SELECT COALESCE(SUM(CASE movement_type WHEN 'ENTRADA' THEN qty_milli ELSE -qty_milli END),0)
        FROM movements WHERE part_id=?''', (part_id,)).fetchone()[0]


def get_part(cn, part_id):
    p = cn.execute('SELECT * FROM parts WHERE id=?', (part_id,)).fetchone()
    if p is None:
        raise HTTPException(404, 'Repuesto no encontrado.')
    return p


def get_vehicle(cn, vehicle_id):
    v = cn.execute('SELECT * FROM vehicles WHERE id=?', (vehicle_id,)).fetchone()
    if v is None:
        raise HTTPException(404, 'Vehículo no encontrado.')
    return v


def service_key(name: str) -> str:
    return ' '.join(name.split()).casefold()


def service_date(value: str) -> str:
    value = value.strip()
    try:
        if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
            raise ValueError()
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise HTTPException(422, 'Fecha inválida: usa una fecha existente con formato AAAA-MM-DD.')


def current_maintenance_plans(cn):
    # History is paginated independently. A service entered late must not replace
    # a more recent effective service, and corrective work cannot reset a plan.
    selected = {}
    ranks = {}
    for row in cn.execute('''SELECT m.*,v.name AS vehicle_name,v.plate
            FROM maintenance m JOIN vehicles v ON v.id=m.vehicle_id
            WHERE m.interval_km>0'''):
        record = dict(row)
        try:
            performed_on = service_date(record['performed_on'])
        except HTTPException:
            # Invalid legacy dates remain available in the history/export.
            continue
        odometer, interval = record['odometer'], record['interval_km']
        if (not isinstance(odometer, int) or not 0 <= odometer <= MAX_ODOMETER_KM
                or not isinstance(interval, int) or not 1 <= interval <= MAX_SERVICE_INTERVAL_KM):
            continue
        normalized_name = service_key(record['service_name'])
        if not normalized_name or normalized_name == service_key(ODOMETER_CORRECTION_SERVICE):
            continue
        key = (record['vehicle_id'], normalized_name)
        rank = (performed_on, odometer, record['id'])
        if key not in ranks or rank > ranks[key]:
            record['performed_on'] = performed_on
            record['service_key'] = normalized_name
            record['next_due_km'] = odometer + interval
            selected[key], ranks[key] = record, rank
    return [selected[key] for key in sorted(selected)]


@app.get('/')
def index():
    return HTMLResponse((ROOT / 'static' / 'index.html').read_text(encoding='utf-8'))


@app.get('/service-worker.js')
def sw():
    return Response((ROOT / 'static' / 'service-worker.js').read_text(encoding='utf-8'), media_type='application/javascript', headers={'Cache-Control':'no-cache','Service-Worker-Allowed':'/'})


@app.get('/manifest.webmanifest')
def manifest():
    return Response((ROOT / 'static' / 'manifest.webmanifest').read_text(encoding='utf-8'), media_type='application/manifest+json')


@app.get('/api/bootstrap')
def bootstrap():
    with connect() as cn:
        vehicles = [dict(r) for r in cn.execute('SELECT * FROM vehicles ORDER BY plate')]
        parts = [dict(r) for r in cn.execute('''SELECT p.*,
          (SELECT COALESCE(SUM(CASE movement_type WHEN 'ENTRADA' THEN qty_milli ELSE -qty_milli END),0) FROM movements WHERE part_id=p.id) AS stock_milli
          FROM parts p ORDER BY p.name''')]
        compat = [dict(r) for r in cn.execute('SELECT * FROM compatibility')]
        inspection = [dict(r) for r in cn.execute('''SELECT i.id,i.client_uuid,i.date,i.inspector,i.odometer,i.vehicle_id,i.failure_count,i.observations,i.created_at,v.plate,v.name AS vehicle_name
            FROM inspections i JOIN vehicles v ON v.id=i.vehicle_id ORDER BY i.id DESC LIMIT 200''')]
        maintenance = [dict(r) for r in cn.execute('''SELECT m.*,v.name AS vehicle_name,v.plate FROM maintenance m JOIN vehicles v ON v.id=m.vehicle_id ORDER BY m.id DESC LIMIT 200''')]
        maintenance_plans = current_maintenance_plans(cn)
        requests = [dict(r) for r in cn.execute('''SELECT q.*,v.plate,v.name AS vehicle_name FROM requests q JOIN vehicles v ON v.id=q.vehicle_id ORDER BY q.id DESC LIMIT 200''')]
        movements = [dict(r) for r in cn.execute('''SELECT m.*,p.code,p.name AS part_name,p.unit,v.plate FROM movements m JOIN parts p ON p.id=m.part_id LEFT JOIN vehicles v ON v.id=m.vehicle_id ORDER BY m.id DESC LIMIT 200''')]
        lines = [dict(r) for r in cn.execute('SELECT * FROM request_lines')]
    return {'vehicles': vehicles, 'parts': parts,'compatibility':compat,'inspections':inspection,'maintenance':maintenance,
            'maintenance_plans':maintenance_plans,
            'requests': requests, 'request_lines':lines,'movements':movements,'template': TEMPLATE,
            'prototype':True}


class VehicleIn(BaseModel):
    plate: str
    name: str
    vehicle_type: str = 'LIVIANO'
    brand: str = ''
    model: str = ''
    odometer: int = Field(default=0, strict=True, ge=0, le=MAX_ODOMETER_KM)
    notes: str = ''


@app.post('/api/vehicles')
def create_vehicle(v: VehicleIn):
    plate = required(v.plate,'Placa').upper()
    name = required(v.name,'Nombre')
    try:
        with connect() as cn:
            cn.execute('INSERT INTO vehicles (plate,name,vehicle_type,brand,model,odometer,notes) VALUES (?,?,?,?,?,?,?)',
                       (plate,name,required(v.vehicle_type,'Tipo'),v.brand.strip(),v.model.strip(),v.odometer,v.notes.strip()))
            return {'ok':True}
    except sqlite3.IntegrityError:
        raise HTTPException(409,'Ya existe un vehículo con esa placa.')


class VehiclePatchIn(BaseModel):
    model_config = {'extra': 'forbid'}
    plate: str | None = None
    name: str | None = None
    vehicle_type: str | None = None
    brand: str | None = None
    model: str | None = None
    odometer: int | None = Field(default=None, strict=True, ge=0, le=MAX_ODOMETER_KM)
    notes: str | None = None
    expected_odometer: int | None = Field(default=None, strict=True, ge=0, le=MAX_EXPECTED_ODOMETER_KM)


@app.patch('/api/vehicles/{vehicle_id}')
def patch_vehicle(vehicle_id:int,v: VehiclePatchIn):
    changes = v.model_dump(exclude_unset=True)
    if any(value is None for value in changes.values()):
        raise HTTPException(422, 'Los campos de una edición no pueden ser nulos; omite los que no deseas modificar.')
    expected_odometer = changes.pop('expected_odometer', None)
    labels = {'plate': 'Placa', 'name': 'Nombre', 'vehicle_type': 'Tipo'}
    for field in tuple(changes):
        if field in labels:
            changes[field] = required(changes[field], labels[field])
            if field == 'plate':
                changes[field] = changes[field].upper()
        elif isinstance(changes[field], str):
            changes[field] = changes[field].strip()
    with connect() as cn:
        cn.execute('BEGIN IMMEDIATE')
        try:
            vehicle = get_vehicle(cn,vehicle_id)
            if expected_odometer is not None and expected_odometer != vehicle['odometer']:
                raise HTTPException(409, 'El kilometraje cambió desde que abriste el formulario. Actualiza los datos y revisa la lectura antes de guardar.')
            if 'odometer' in changes and changes['odometer'] < vehicle['odometer']:
                raise HTTPException(409, 'La edición normal no puede reducir el kilometraje actual. Usa la acción explícita de corrección e indica el motivo y responsable.')
            if changes:
                # Column names come exclusively from the validated patch model.
                columns = ','.join(f'{field}=?' for field in changes)
                cn.execute(f'UPDATE vehicles SET {columns} WHERE id=?', (*changes.values(), vehicle_id))
            cn.execute('COMMIT')
            return {'ok':True, 'odometer':changes.get('odometer', vehicle['odometer'])}
        except sqlite3.IntegrityError:
            cn.execute('ROLLBACK')
            raise HTTPException(409,'La placa ya está registrada.')
        except Exception:
            cn.execute('ROLLBACK')
            raise


class OdometerCorrectionIn(BaseModel):
    model_config = {'extra': 'forbid'}
    odometer: int = Field(strict=True, ge=0, le=MAX_ODOMETER_KM)
    expected_odometer: int = Field(strict=True, ge=0, le=MAX_EXPECTED_ODOMETER_KM)
    reason: str
    responsible: str
    corrected_on: str


@app.post('/api/vehicles/{vehicle_id}/odometer-correction')
def correct_odometer(vehicle_id: int, payload: OdometerCorrectionIn):
    reason = required(payload.reason, 'Motivo de la corrección', 500)
    responsible = required(payload.responsible, 'Responsable de la corrección', 100)
    corrected_on = service_date(payload.corrected_on)
    with connect() as cn:
        cn.execute('BEGIN IMMEDIATE')
        try:
            vehicle = get_vehicle(cn, vehicle_id)
            if vehicle['odometer'] != payload.expected_odometer:
                raise HTTPException(409, 'El kilometraje cambió desde que abriste la corrección. Actualiza los datos y revisa la lectura antes de guardar.')
            if vehicle['odometer'] == payload.odometer:
                raise HTTPException(422, 'La lectura corregida debe ser diferente de la lectura actual.')
            notes = (f"Lectura anterior: {vehicle['odometer']} km.\n"
                     f'Lectura corregida: {payload.odometer} km.\n'
                     f'Responsable: {responsible}\nMotivo: {reason}')
            # Append an audit event in existing history; never rewrite previous
            # readings or create a preventive plan from an odometer correction.
            cur = cn.execute('''INSERT INTO maintenance
                (vehicle_id,service_name,performed_on,odometer,interval_km,next_due_km,notes)
                VALUES(?,?,?,?,NULL,NULL,?)''', (vehicle_id, ODOMETER_CORRECTION_SERVICE,
                    corrected_on, payload.odometer, notes))
            cn.execute('UPDATE vehicles SET odometer=? WHERE id=?', (payload.odometer, vehicle_id))
            cn.execute('COMMIT')
            return {'ok':True, 'id':cur.lastrowid, 'odometer':payload.odometer}
        except Exception:
            cn.execute('ROLLBACK')
            raise


class InspectionIn(BaseModel):
    client_uuid: str
    vehicle_id: int
    date: str
    inspector: str
    odometer: int = Field(ge=0)
    answers: dict[str,str]
    observations: str = ''
    photos: list[str] = Field(default_factory=list)


@app.post('/api/inspections')
def save_inspection(payload: InspectionIn):
    uid = required(payload.client_uuid,'ID de inspección',80)
    if len(uid) < 8:
        raise HTTPException(422,'Identificador de inspección inválido.')
    if set(payload.answers.keys()) != CHECK_IDS or any(x not in ('BIEN','MAL','NA') for x in payload.answers.values()):
        raise HTTPException(422,'Responde cada ítem del checklist con BIEN, MAL o N/A.')
    try:
        date.fromisoformat(payload.date)
    except ValueError:
        raise HTTPException(422,'Fecha inválida.')
    if len(payload.observations) > 2500:
        raise HTTPException(422,'Las observaciones y notas del checklist no pueden superar 2500 caracteres. Reduce el texto antes de finalizar; no se ha guardado la inspección.')
    if len(payload.photos)>3:
        raise HTTPException(422,'Máximo tres fotografías JPEG reducidas de tamaño.')
    for photo in payload.photos:
        if len(photo)>350_000 or not re.fullmatch(r'data:image/jpeg;base64,[A-Za-z0-9+/=]+',photo):
            raise HTTPException(422,'Formato de fotografía inválido.')
        try:
            data=base64.b64decode(photo.split(',',1)[1],validate=True)
            if not data.startswith(b'\xff\xd8'):
                raise ValueError()
        except (ValueError,base64.binascii.Error):
            raise HTTPException(422,'La fotografía no es un JPEG válido.')
    with connect() as cn:
        cn.execute('BEGIN IMMEDIATE')
        try:
            previous=cn.execute('SELECT id FROM inspections WHERE client_uuid=?',(uid,)).fetchone()
            if previous:
                cn.execute('COMMIT')
                return {'ok':True,'id':previous['id'],'duplicate':True}
            v=get_vehicle(cn,payload.vehicle_id)
            if v['vehicle_type'] != 'LIVIANO':
                raise HTTPException(422,'El checklist disponible actualmente corresponde solo a vehículos livianos.')
            cur=cn.execute('''INSERT INTO inspections(client_uuid,vehicle_id,date,inspector,odometer,answers_json,observations,photos_json,failure_count)
                VALUES(?,?,?,?,?,?,?,?,?)''',(uid,payload.vehicle_id,payload.date,required(payload.inspector,'Inspector'),payload.odometer,
                json.dumps(payload.answers,ensure_ascii=False),payload.observations,json.dumps(payload.photos),
                sum(1 for a in payload.answers.values() if a=='MAL')))
            cn.execute('UPDATE vehicles SET odometer=MAX(odometer,?) WHERE id=?',(payload.odometer,payload.vehicle_id))
            cn.execute('COMMIT')
            return {'ok':True,'id':cur.lastrowid}
        except Exception:
            cn.execute('ROLLBACK')
            raise


@app.get('/api/inspections/{inspection_id}')
def inspection_detail(inspection_id:int):
    r=row('''SELECT i.*,v.plate,v.name AS vehicle_name FROM inspections i JOIN vehicles v ON v.id=i.vehicle_id WHERE i.id=?''',(inspection_id,))
    if not r:raise HTTPException(404,'Inspección no encontrada.')
    r['answers']=json.loads(r.pop('answers_json'))
    r['photos']=json.loads(r.pop('photos_json'))
    r['template']=TEMPLATE
    return r


class PartIn(BaseModel):
    code:str
    name:str
    part_number:str=''
    category:str='Filtros'
    unit:str='unidad'
    min_qty:str='0'
    notes:str=''
    compatible_vehicle_ids:list[int]=Field(default_factory=list)


@app.post('/api/parts')
def create_part(p: PartIn):
    try:
        minq=Decimal(str(p.min_qty))
        if not minq.is_finite() or minq<0 or minq*1000!=(minq*1000).to_integral_value():raise ValueError()
        minm=int(minq*1000)
        if minm>2_000_000_000_000:raise ValueError()
    except (ValueError,InvalidOperation):
        raise HTTPException(422,'Stock mínimo inválido (máximo tres decimales).')
    with connect() as cn:
        cn.execute('BEGIN IMMEDIATE')
        try:
            cur=cn.execute('INSERT INTO parts (code,name,part_number,category,unit,min_milli,notes) VALUES(?,?,?,?,?,?,?)',
                (required(p.code,'Código').upper(),required(p.name,'Repuesto'),p.part_number.strip(),required(p.category,'Categoría'),required(p.unit,'Unidad'),minm,p.notes.strip()))
            for vid in set(p.compatible_vehicle_ids):
                get_vehicle(cn,vid)
                cn.execute('INSERT INTO compatibility(vehicle_id,part_id) VALUES (?,?)',(vid,cur.lastrowid))
            cn.execute('COMMIT')
            return {'ok':True,'id':cur.lastrowid}
        except sqlite3.IntegrityError:
            cn.execute('ROLLBACK')
            raise HTTPException(409,'Código de repuesto duplicado.')
        except Exception:
            cn.execute('ROLLBACK')
            raise


class MoveIn(BaseModel):
    part_id:int
    movement_type:str
    quantity:str
    responsible:str
    vehicle_id:int|None=None
    reference:str=''
    notes:str=''


def inventory_fingerprint(payload: BaseModel) -> str:
    # Include model defaults so omitted/default fields have the same meaning.
    content = json.dumps(payload.model_dump(mode='json'), sort_keys=True,
                         separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def replay_inventory_operation(cn, operation_key: str, scope: str, request_hash: str):
    previous = cn.execute('''SELECT scope,request_hash,response_json
        FROM inventory_operations WHERE operation_key=?''', (operation_key,)).fetchone()
    if previous is None:
        return None
    if previous['scope'] != scope or previous['request_hash'] != request_hash:
        raise HTTPException(409, {
            'code': 'idempotency_conflict',
            'message': 'Esta clave de operación ya fue utilizada con datos diferentes. Conserva la clave y revisa la operación antes de volver a enviar.',
        })
    return json.loads(previous['response_json'])


def record_inventory_operation(cn, operation_key: str, scope: str, request_hash: str, result: dict):
    # Called within the SAME write transaction as stock/request updates. A failed
    # insert or commit rolls back every effect; rejected operations retain no key.
    cn.execute('''INSERT INTO inventory_operations(operation_key,scope,request_hash,response_json)
        VALUES(?,?,?,?)''', (operation_key, scope, request_hash,
        json.dumps(result, separators=(',', ':'), ensure_ascii=False)))


@app.post('/api/movements')
def create_movement(m: MoveIn, idempotency_key: str = Header(
        ..., alias='Idempotency-Key', min_length=1, max_length=128,
        pattern=r'^[A-Za-z0-9._:-]+$')):
    scope = 'movement'
    request_hash = inventory_fingerprint(m)
    with connect() as cn:
        cn.execute('BEGIN IMMEDIATE')
        try:
            previous = replay_inventory_operation(cn, idempotency_key, scope, request_hash)
            if previous is not None:
                cn.execute('COMMIT')
                return previous
            if m.movement_type not in ('ENTRADA','SALIDA'):
                raise HTTPException(422,'El movimiento debe ser ENTRADA o SALIDA.')
            q=milli(m.quantity)
            p=get_part(cn,m.part_id)
            if p['unit']=='unidad' and q%1000:
                raise HTTPException(422,'Los repuestos contados por unidad requieren cantidades enteras.')
            if m.vehicle_id is not None:get_vehicle(cn,m.vehicle_id)
            if m.movement_type=='SALIDA' and q>stock(cn,m.part_id):
                raise HTTPException(409,'No hay existencias suficientes para esta salida.')
            cur=cn.execute('''INSERT INTO movements(part_id,movement_type,qty_milli,vehicle_id,reference,responsible,notes)
                VALUES(?,?,?,?,?,?,?)''',(m.part_id,m.movement_type,q,m.vehicle_id,m.reference.strip(),required(m.responsible,'Responsable'),m.notes.strip()))
            result = {'ok':True,'id':cur.lastrowid}
            record_inventory_operation(cn, idempotency_key, scope, request_hash, result)
            cn.execute('COMMIT')
            return result
        except Exception:
            cn.execute('ROLLBACK')
            raise


class MaintenanceIn(BaseModel):
    vehicle_id:int
    service_name:str
    performed_on:str
    odometer:int=Field(strict=True,ge=0,le=MAX_ODOMETER_KM)
    interval_km:int|None=Field(default=None,strict=True,gt=0,le=MAX_SERVICE_INTERVAL_KM)
    notes:str=''


@app.post('/api/maintenance')
def create_maintenance(m:MaintenanceIn):
    service_name = required(m.service_name,'Servicio')
    if service_key(service_name) == service_key(ODOMETER_CORRECTION_SERVICE):
        raise HTTPException(422, 'Las correcciones de kilometraje deben registrarse mediante la acción explícita de corrección del vehículo.')
    performed_on = service_date(m.performed_on)
    with connect() as cn:
        cn.execute('BEGIN IMMEDIATE')
        try:
            get_vehicle(cn,m.vehicle_id)
            cur=cn.execute('''INSERT INTO maintenance(vehicle_id,service_name,performed_on,odometer,interval_km,next_due_km,notes)
                VALUES(?,?,?,?,?,?,?)''',(m.vehicle_id,service_name,performed_on,m.odometer,
                m.interval_km,m.odometer + m.interval_km if m.interval_km else None,m.notes.strip()))
            cn.execute('UPDATE vehicles SET odometer=MAX(odometer,?) WHERE id=?',(m.odometer,m.vehicle_id))
            cn.execute('COMMIT')
            return {'ok':True,'id':cur.lastrowid}
        except Exception:
            cn.execute('ROLLBACK')
            raise


class RequestItem(BaseModel):
    part_id:int
    quantity:str


class RequestIn(BaseModel):
    vehicle_id:int
    requester:str
    reason:str=''
    items:list[RequestItem]=Field(min_length=1)


@app.post('/api/requests')
def create_request(r:RequestIn):
    with connect() as cn:
        cn.execute('BEGIN IMMEDIATE')
        try:
            get_vehicle(cn,r.vehicle_id)
            if len({x.part_id for x in r.items})!=len(r.items):
                raise HTTPException(422,'Un repuesto no debe repetirse en la misma solicitud.')
            cur=cn.execute('INSERT INTO requests(vehicle_id,requester,reason) VALUES(?,?,?)',
                (r.vehicle_id,required(r.requester,'Solicitante'),r.reason.strip()))
            for x in r.items:
                p=get_part(cn,x.part_id); qty=milli(x.quantity)
                if p['unit']=='unidad' and qty%1000:raise HTTPException(422,'Cantidad fraccionaria no permitida para unidades.')
                cn.execute('INSERT INTO request_lines(request_id,part_id,qty_milli) VALUES(?,?,?)',(cur.lastrowid,x.part_id,qty))
            cn.execute('COMMIT')
            return {'ok':True,'id':cur.lastrowid}
        except Exception:
            cn.execute('ROLLBACK');raise


class ApprovalIn(BaseModel):
    approved_by:str


@app.post('/api/requests/{request_id}/approve')
def approve(request_id:int, payload:ApprovalIn):
    with connect() as cn:
        cn.execute('BEGIN IMMEDIATE')
        try:
            q=cn.execute('SELECT * FROM requests WHERE id=?',(request_id,)).fetchone()
            if not q:raise HTTPException(404,'Solicitud no encontrada.')
            if q['status']!='PENDIENTE':raise HTTPException(409,'Esta solicitud ya no está pendiente.')
            cn.execute('UPDATE requests SET status=?,approved_by=? WHERE id=?',('APROBADA',required(payload.approved_by,'Nombre de quien autoriza'),request_id))
            cn.execute('COMMIT');return {'ok':True}
        except Exception:
            cn.execute('ROLLBACK');raise


class DeliveryLine(BaseModel):
    line_id:int
    quantity:str

class DeliveryIn(BaseModel):
    received_by:str
    delivered_by:str
    lines:list[DeliveryLine]=Field(min_length=1)


@app.post('/api/requests/{request_id}/deliver')
def deliver(request_id:int,payload:DeliveryIn, idempotency_key: str = Header(
        ..., alias='Idempotency-Key', min_length=1, max_length=128,
        pattern=r'^[A-Za-z0-9._:-]+$')):
    scope = f'delivery:{request_id}'
    request_hash = inventory_fingerprint(payload)
    with connect() as cn:
        cn.execute('BEGIN IMMEDIATE')
        try:
            previous = replay_inventory_operation(cn, idempotency_key, scope, request_hash)
            if previous is not None:
                cn.execute('COMMIT')
                return previous
            req=cn.execute('SELECT * FROM requests WHERE id=?',(request_id,)).fetchone()
            if not req:raise HTTPException(404,'Solicitud no encontrada.')
            if req['status'] not in ('APROBADA','PARCIAL'):
                raise HTTPException(409,'La solicitud debe estar autorizada antes de entregar.')
            if len({x.line_id for x in payload.lines})!=len(payload.lines):
                raise HTTPException(422,'Línea de solicitud duplicada.')
            responsible=required(payload.delivered_by,'Responsable de entrega')
            receiver=required(payload.received_by,'Nombre de quien recibe')
            for x in payload.lines:
                line=cn.execute('SELECT * FROM request_lines WHERE id=? AND request_id=?',(x.line_id,request_id)).fetchone()
                if not line:raise HTTPException(422,'La línea no pertenece a esta solicitud.')
                n=milli(x.quantity)
                if line['delivered_milli']+n>line['qty_milli']:
                    raise HTTPException(422,'La entrega excede la cantidad solicitada.')
                p=get_part(cn,line['part_id'])
                if p['unit']=='unidad' and n%1000:raise HTTPException(422,'Cantidad fraccionaria no permitida para unidades.')
                if stock(cn,line['part_id'])<n:
                    raise HTTPException(409,f'Existencias insuficientes para {p["name"]}.')
                cn.execute('''INSERT INTO movements(part_id,movement_type,qty_milli,vehicle_id,reference,responsible,notes)
                    VALUES(?,'SALIDA',?,?,?,?,?)''',(line['part_id'],n,req['vehicle_id'],f'SOL-{request_id:05d}',responsible,f'Entregado a {receiver}'))
                cn.execute('UPDATE request_lines SET delivered_milli=delivered_milli+? WHERE id=?',(n,x.line_id))
            pending=cn.execute('''SELECT COUNT(*) FROM request_lines WHERE request_id=? AND delivered_milli<qty_milli''',(request_id,)).fetchone()[0]
            cn.execute('UPDATE requests SET status=?,received_by=? WHERE id=?',('PARCIAL' if pending else 'ENTREGADA',receiver,request_id))
            result = {'ok':True,'status':'PARCIAL' if pending else 'ENTREGADA'}
            record_inventory_operation(cn, idempotency_key, scope, request_hash, result)
            cn.execute('COMMIT');return result
        except Exception:
            cn.execute('ROLLBACK');raise


@app.get('/api/requests/{request_id}')
def request_detail(request_id:int):
    req=row('SELECT * FROM requests WHERE id=?',(request_id,))
    if not req:raise HTTPException(404,'Solicitud no encontrada.')
    req['lines']=rows('''SELECT l.*,p.name,p.code,p.unit FROM request_lines l JOIN parts p ON p.id=l.part_id WHERE l.request_id=?''',(request_id,))
    return req


def make_csv(header, values, filename):
    buf=StringIO();w=csv.writer(buf,delimiter=';');w.writerow(header)
    for val in values:
        # Excel can evaluate formulas in untrusted CSV cells; prefix dangerous strings.
        safe=[]
        for cell in val:
            if isinstance(cell,str) and cell.lstrip().startswith(('=','+','-','@')):
                cell="'"+cell
            safe.append(cell)
        w.writerow(safe)
    return Response(content='\ufeff'+buf.getvalue(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="{filename}"'})


@app.get('/api/export/{what}')
def export_csv(what:str):
    if what=='vehicles':
        r=rows('SELECT plate,name,vehicle_type,brand,model,odometer,notes FROM vehicles ORDER BY plate')
        return make_csv(['Placa','Vehículo','Tipo','Marca','Modelo','Km','Notas'],[x.values() for x in r],'vehiculos.csv')
    if what=='inspections':
        r=rows('''SELECT i.date,v.plate,i.inspector,i.odometer,i.failure_count,i.observations,i.client_uuid FROM inspections i JOIN vehicles v ON v.id=i.vehicle_id ORDER BY i.id''')
        return make_csv(['Fecha','Placa','Inspector','Km','Fallas','Observaciones','ID'],[x.values() for x in r],'inspecciones.csv')
    if what=='movements':
        r=rows('''SELECT m.created_at,p.code,p.name,m.movement_type,m.qty_milli,p.unit,COALESCE(v.plate,''),m.reference,m.responsible FROM movements m JOIN parts p ON p.id=m.part_id LEFT JOIN vehicles v ON v.id=m.vehicle_id ORDER BY m.id''')
        return make_csv(['Fecha','Código','Repuesto','Tipo','Cantidad (milésimas)','Unidad','Placa destino','Documento','Responsable'],[x.values() for x in r],'movimientos.csv')
    if what=='maintenance':
        r=rows('''SELECT m.performed_on,v.plate,m.service_name,m.odometer,m.interval_km,m.next_due_km,m.notes FROM maintenance m JOIN vehicles v ON v.id=m.vehicle_id ORDER BY m.id''')
        return make_csv(['Fecha','Placa','Servicio','Km','Intervalo km','Próximo km','Notas'],[x.values() for x in r],'mantenimientos.csv')
    raise HTTPException(404,'Exportación no disponible.')


@app.get('/api/backup')
def backup():
    with connect() as cn:
        tmp=sqlite3.connect(':memory:')
        try:
            cn.backup(tmp)
            # Serialize snapshot using a temporary file; only for trusted local prototype usage.
            with TemporaryDirectory() as folder:
                path=Path(folder)/'snapshot.db'
                file_db=sqlite3.connect(str(path))
                try:
                    tmp.backup(file_db)
                finally:
                    file_db.close()
                data=path.read_bytes()
        finally:tmp.close()
    filename=f'goldengine_fleet_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db'
    return Response(data,media_type='application/octet-stream',headers={'Content-Disposition':f'attachment; filename="{filename}"'})


@app.get('/api/health')
def health():
    return {'ok':True,'prototype':True}
