"""Gold Engine Fleet: local FastAPI + SQLite PWA prototype.
Not designed to be exposed on the public Internet without authentication and HTTPS.
"""
from __future__ import annotations

import csv
import base64
import argparse
from contextlib import contextmanager
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
from tempfile import TemporaryDirectory
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'


def resolve_database_path(value):
    if not str(value).strip():
        raise RuntimeError('La ruta de la base no puede estar vacía. Revisa GEFLEET_DB.')
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RuntimeError('La ruta de la base debe ser absoluta. Revisa GEFLEET_DB o --database.')
    return path.resolve()


# Validate when opening/starting rather than during import so a CLI
# --database argument can explicitly correct an invalid environment value.
DB = os.environ.get('GEFLEET_DB', str(DATA / 'fleet.db'))
TEMPLATE = json.loads((ROOT / 'checklist_liviano.json').read_text(encoding='utf-8'))
CHECK_IDS = {f'{gi}-{ii}' for gi, g in enumerate(TEMPLATE) for ii, _ in enumerate(g['items'])}
SCHEMA_VERSION = 2
MAX_ODOMETER_KM = 10_000_000
MAX_SERVICE_INTERVAL_KM = 1_000_000
MAX_EXPECTED_ODOMETER_KM = 9_007_199_254_740_991
ODOMETER_CORRECTION_SERVICE = 'Corrección de kilometraje'

app = FastAPI(title='Gold Engine Fleet', docs_url='/api/docs', redoc_url=None)
app.mount('/static', StaticFiles(directory=str(ROOT / 'static')), name='static')


@contextmanager
def _open_database(path, mode='rw', wal=False):
    # mode=rw is deliberate: an incorrect/deleted path must never look like an
    # empty fleet. Creation is restricted to explicit database initialization.
    path = resolve_database_path(path)
    if not path.is_file():
        raise RuntimeError(f'No existe la base de datos: {path}. Revisa GEFLEET_DB. '
                           'Para una instalación nueva usa server.py db-init.')
    cn = sqlite3.connect(path.as_uri() + f'?mode={mode}', uri=True, timeout=15, isolation_level=None)
    try:
        cn.row_factory = sqlite3.Row
        cn.execute('PRAGMA foreign_keys=ON')
        cn.execute('PRAGMA busy_timeout=15000')
        if wal:
            cn.execute('PRAGMA journal_mode=WAL')
        yield cn
        cn.commit()
    except Exception:
        cn.rollback()
        raise
    finally:
        cn.close()


def connect():
    return _open_database(DB, wal=True)


BASE_SCHEMA = '''
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
'''

BASE_TABLE_COLUMNS = {
    'vehicles': 'id plate name vehicle_type model brand odometer notes active created_at',
    'inspections': 'id client_uuid vehicle_id date inspector odometer answers_json observations photos_json failure_count created_at',
    'parts': 'id code name part_number category unit min_milli notes created_at',
    'compatibility': 'vehicle_id part_id notes',
    'movements': 'id part_id movement_type qty_milli vehicle_id reference responsible notes created_at',
    'maintenance': 'id vehicle_id service_name performed_on odometer interval_km next_due_km notes created_at',
    'requests': 'id vehicle_id requester reason status approved_by received_by created_at',
    'request_lines': 'id request_id part_id qty_milli delivered_milli',
}
INVENTORY_OPERATION_COLUMNS = 'operation_key scope request_hash response_json created_at'
MAINTENANCE_DETAIL_COLUMNS = 'maintenance_type work_performed responsible location'
BASE_FOREIGN_KEYS = {
    'inspections': {('vehicle_id', 'vehicles', 'id')},
    'compatibility': {('vehicle_id', 'vehicles', 'id'), ('part_id', 'parts', 'id')},
    'movements': {('part_id', 'parts', 'id'), ('vehicle_id', 'vehicles', 'id')},
    'maintenance': {('vehicle_id', 'vehicles', 'id')},
    'requests': {('vehicle_id', 'vehicles', 'id')},
    'request_lines': {('request_id', 'requests', 'id'), ('part_id', 'parts', 'id')},
}


def _migration_1(cn):
    # Preserve the existing additive 0 -> 1 migration and every historical row.
    cn.execute('''CREATE TABLE inventory_operations (
                    operation_key TEXT PRIMARY KEY NOT NULL CHECK(length(operation_key) BETWEEN 1 AND 128),
                    scope TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )''')


def _migration_2(cn):
    # Add service details without rewriting notes or any historical record.
    for column in MAINTENANCE_DETAIL_COLUMNS.split():
        cn.execute(f"ALTER TABLE maintenance ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    cn.execute('''CREATE INDEX idx_maintenance_vehicle_date
                  ON maintenance(vehicle_id,performed_on DESC,odometer DESC,id DESC)''')


MIGRATIONS = {1: _migration_1, 2: _migration_2}


def _validate_schema(cn, version):
    if version < 0 or version > SCHEMA_VERSION:
        raise RuntimeError('La base utiliza una versión de esquema no compatible con esta aplicación.')
    expected = dict(BASE_TABLE_COLUMNS)
    if version >= 1:
        expected['inventory_operations'] = INVENTORY_OPERATION_COLUMNS
    if version >= 2:
        expected['maintenance'] += ' ' + MAINTENANCE_DETAIL_COLUMNS
    tables = {r[0] for r in cn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if version == 0 and 'inventory_operations' in tables:
        raise RuntimeError('El esquema declara versión 0 pero contiene inventory_operations. '
                           'Revisa la versión y el origen de la base antes de continuar.')
    for table, names in expected.items():
        if table not in tables:
            raise RuntimeError(f'Esquema incompleto: falta la tabla {table}. No se modifica la base.')
        columns = {r[1] for r in cn.execute(f'PRAGMA table_info("{table}")')}
        if not set(names.split()) <= columns:
            raise RuntimeError(f'Esquema incompatible en {table}: faltan columnas. No se modifica la base.')
    for table, expected_keys in BASE_FOREIGN_KEYS.items():
        keys = {(r[3], r[2], r[4]) for r in cn.execute(f'PRAGMA foreign_key_list("{table}")')}
        if not expected_keys <= keys:
            raise RuntimeError(f'Esquema incompatible en {table}: faltan claves foráneas.')
    if version >= 1:
        columns = {r[1]: r for r in cn.execute('PRAGMA table_info(inventory_operations)')}
        primary_key = [name for name, column in columns.items() if column[5]]
        if primary_key != ['operation_key'] or not columns['operation_key'][3]:
            raise RuntimeError('inventory_operations no tiene la clave única requerida.')


def validate_database(cn, require_current=True):
    version = cn.execute('PRAGMA user_version').fetchone()[0]
    _validate_schema(cn, version)
    if require_current and version != SCHEMA_VERSION:
        raise RuntimeError('La base necesita una migración antes de utilizarse.')
    integrity = [r[0] for r in cn.execute('PRAGMA integrity_check')]
    if integrity != ['ok']:
        raise RuntimeError('La comprobación de integridad SQLite falló: ' + '; '.join(integrity))
    foreign_keys = [tuple(r) for r in cn.execute('PRAGMA foreign_key_check')]
    if foreign_keys:
        raise RuntimeError(f'La base contiene {len(foreign_keys)} violaciones de claves foráneas.')
    return version


def _migration_authorizer(action, arg1, arg2, database, trigger):
    # executescript() commits implicitly before running its script. Deny that
    # commit, transaction control, and schema-version writes inside callbacks.
    if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
                  sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA and arg2 is not None:
        if arg1.casefold() in ('user_version', 'foreign_keys', 'journal_mode', 'writable_schema'):
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def apply_migrations(cn, initialize=False):
    if cn.in_transaction:
        raise RuntimeError('El ejecutor de migraciones requiere una conexión sin transacción activa.')
    # Check the entire registry before the first write, including future gaps.
    if set(MIGRATIONS) != set(range(1, SCHEMA_VERSION + 1)):
        raise RuntimeError('Registro de migraciones incompleto: deben ser consecutivas desde la versión 1.')
    cn.execute('BEGIN IMMEDIATE')
    try:
        version = cn.execute('PRAGMA user_version').fetchone()[0]
        if initialize:
            if version != 0 or cn.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone():
                raise RuntimeError('La inicialización requiere una base nueva y vacía.')
            # This static schema has no triggers; execute each complete statement
            # separately to keep schema creation inside the same transaction.
            for statement in BASE_SCHEMA.split(';'):
                if statement.strip():
                    cn.execute(statement)
        _validate_schema(cn, version)
        for target in range(version + 1, SCHEMA_VERSION + 1):
            cn.set_authorizer(_migration_authorizer)
            try:
                MIGRATIONS[target](cn)
            finally:
                cn.set_authorizer(None)
            if not cn.in_transaction:
                raise RuntimeError('Una migración cerró su transacción antes de terminar.')
            cn.execute(f'PRAGMA user_version={target}')
        validate_database(cn)
        cn.execute('COMMIT')
    except BaseException:
        cn.set_authorizer(None)
        if cn.in_transaction:
            cn.execute('ROLLBACK')
        raise


def init_db(allow_create=None):
    path = resolve_database_path(DB)
    if allow_create is None:
        allow_create = os.environ.get('GEFLEET_ALLOW_CREATE_DB') == '1'
    if not path.exists():
        if not allow_create:
            raise RuntimeError(f'No existe la base de datos: {path}. Revisa GEFLEET_DB; '
                               'para crear una base nueva ejecuta server.py db-init.')
        if not path.parent.is_dir():
            raise RuntimeError(f'La carpeta de la base no existe: {path.parent}. Créala explícitamente.')
        # O_EXCL prevents a concurrent initializer from overwriting an existing
        # file. A failed initialization may leave an empty file for inspection.
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with _open_database(path) as cn:
            apply_migrations(cn, initialize=True)
            cn.execute('PRAGMA journal_mode=WAL')
    else:
        with _open_database(path) as cn:
            apply_migrations(cn)
            cn.execute('PRAGMA journal_mode=WAL')


@app.on_event('startup')
def _startup():
    init_db()
    logging.getLogger('uvicorn.error').info('Base SQLite activa: %s; esquema: %s', resolve_database_path(DB), SCHEMA_VERSION)


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
            WHERE m.interval_km>0 AND m.maintenance_type<>'CORRECTIVO' '''):
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
    vehicle_id:int=Field(strict=True,gt=0,le=MAX_EXPECTED_ODOMETER_KM)
    service_name:str=Field(max_length=250)
    performed_on:str
    odometer:int=Field(strict=True,ge=0,le=MAX_ODOMETER_KM)
    interval_km:int|None=Field(default=None,strict=True,gt=0,le=MAX_SERVICE_INTERVAL_KM)
    notes:str=Field(default='',max_length=4000)
    maintenance_type:Literal['','MPG','CORRECTIVO']=''
    work_performed:str=Field(default='',max_length=4000)
    responsible:str=Field(default='',max_length=150)
    location:str=Field(default='',max_length=150)


@app.post('/api/maintenance')
def create_maintenance(m:MaintenanceIn):
    service_name = required(m.service_name,'Servicio')
    if service_key(service_name) == service_key(ODOMETER_CORRECTION_SERVICE):
        raise HTTPException(422, 'Las correcciones de kilometraje deben registrarse mediante la acción explícita de corrección del vehículo.')
    if m.maintenance_type == 'CORRECTIVO' and m.interval_km is not None:
        raise HTTPException(422, 'Un mantenimiento correctivo no puede reiniciar un plan preventivo. Deja vacío el intervalo.')
    performed_on = service_date(m.performed_on)
    with connect() as cn:
        cn.execute('BEGIN IMMEDIATE')
        try:
            get_vehicle(cn,m.vehicle_id)
            cur=cn.execute('''INSERT INTO maintenance(vehicle_id,service_name,performed_on,odometer,interval_km,next_due_km,
                notes,maintenance_type,work_performed,responsible,location)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(m.vehicle_id,service_name,performed_on,m.odometer,
                m.interval_km,m.odometer + m.interval_km if m.interval_km else None,m.notes.strip(),
                m.maintenance_type,m.work_performed.strip(),m.responsible.strip(),m.location.strip()))
            cn.execute('UPDATE vehicles SET odometer=MAX(odometer,?) WHERE id=?',(m.odometer,m.vehicle_id))
            cn.execute('COMMIT')
            return {'ok':True,'id':cur.lastrowid}
        except Exception:
            cn.execute('ROLLBACK')
            raise


@app.get('/api/maintenance')
def maintenance_history(
        vehicle_id:int|None=Query(default=None,gt=0,le=MAX_EXPECTED_ODOMETER_KM),
        date_from:str|None=None,
        date_to:str|None=None,
        maintenance_type:Literal['MPG','CORRECTIVO','LEGACY']|None=None,
        page:int=Query(default=1,ge=1,le=2_147_483_647),
        page_size:int=Query(default=20,ge=1,le=100)):
    start = service_date(date_from) if date_from is not None else None
    end = service_date(date_to) if date_to is not None else None
    if start is not None and end is not None and start > end:
        raise HTTPException(422, 'La fecha inicial no puede ser posterior a la fecha final.')
    conditions, values = [], []
    if vehicle_id is not None:
        conditions.append('m.vehicle_id=?')
        values.append(vehicle_id)
    if start is not None:
        conditions.append('m.performed_on>=?')
        values.append(start)
    if end is not None:
        conditions.append('m.performed_on<=?')
        values.append(end)
    if maintenance_type is not None:
        conditions.append('m.maintenance_type=?')
        values.append('' if maintenance_type == 'LEGACY' else maintenance_type)
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    joined = ' FROM maintenance m JOIN vehicles v ON v.id=m.vehicle_id'
    order = ' ORDER BY m.performed_on DESC,m.odometer DESC,m.id DESC'
    with connect() as cn:
        # Count, page, and current plans are one consistent read snapshot.
        cn.execute('BEGIN')
        if vehicle_id is not None:
            get_vehicle(cn, vehicle_id)
        total = cn.execute('SELECT COUNT(*)' + joined + where, values).fetchone()[0]
        items = [dict(row) for row in cn.execute(
            'SELECT m.*,v.name AS vehicle_name,v.plate' + joined + where + order + ' LIMIT ? OFFSET ?',
            [*values, page_size, (page - 1) * page_size])]
        latest_where = ' WHERE m.vehicle_id=?' if vehicle_id is not None else ''
        latest_values = (vehicle_id,) if vehicle_id is not None else ()
        latest = None
        for record in cn.execute('SELECT m.*,v.name AS vehicle_name,v.plate' + joined + latest_where + order,
                                 latest_values):
            if service_key(record['service_name']) == service_key(ODOMETER_CORRECTION_SERVICE):
                continue
            try:
                service_date(record['performed_on'])
            except HTTPException:
                continue
            latest = dict(record)
            break
        plans = current_maintenance_plans(cn)
        if vehicle_id is not None:
            plans = [plan for plan in plans if plan['vehicle_id'] == vehicle_id]
        current_odometer = {vehicle['id']: vehicle['odometer']
                            for vehicle in cn.execute('SELECT id,odometer FROM vehicles')}
        plans = [{**plan, 'current_odometer':current_odometer[plan['vehicle_id']]}
                 for plan in plans]
    return {'items':items,'total':total,'page':page,'page_size':page_size,
            'pages':(total + page_size - 1) // page_size,'latest':latest,'plans':plans}


@app.get('/api/maintenance/{maintenance_id}')
def maintenance_detail(maintenance_id:int):
    if not 1 <= maintenance_id <= MAX_EXPECTED_ODOMETER_KM:
        raise HTTPException(422, 'El identificador del mantenimiento está fuera de rango.')
    record = row('''SELECT m.*,v.name AS vehicle_name,v.plate FROM maintenance m
                    JOIN vehicles v ON v.id=m.vehicle_id WHERE m.id=?''', (maintenance_id,))
    if record is None:
        raise HTTPException(404, 'Mantenimiento no encontrado.')
    return record


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
        r=rows('''SELECT m.performed_on,v.plate,m.service_name,m.maintenance_type,m.work_performed,
            m.odometer,m.interval_km,m.next_due_km,m.responsible,m.location,m.notes
            FROM maintenance m JOIN vehicles v ON v.id=m.vehicle_id
            ORDER BY m.performed_on DESC,m.odometer DESC,m.id DESC''')
        return make_csv(['Fecha','Placa','Servicio','Tipo de mantenimiento','Trabajo realizado','Km',
                         'Intervalo km','Próximo km','Responsable','Lugar','Observaciones'],
                        [x.values() for x in r],'mantenimientos.csv')
    raise HTTPException(404,'Exportación no disponible.')


@app.get('/api/backup')
def backup():
    with TemporaryDirectory() as folder:
        path = Path(folder) / 'snapshot.db'
        create_backup(path)
        data = path.read_bytes()
    filename=f'goldengine_fleet_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db'
    return Response(data,media_type='application/octet-stream',headers={'Content-Disposition':f'attachment; filename="{filename}"'})


@app.get('/api/health')
def health():
    with _open_database(DB, mode='ro') as cn:
        version = cn.execute('PRAGMA user_version').fetchone()[0]
    return {'ok':True,'prototype':True,'database':str(resolve_database_path(DB)),
            'schema_version':version}


def database_info(path=None):
    path = resolve_database_path(DB if path is None else path)
    with _open_database(path, mode='ro') as cn:
        cn.execute('BEGIN')
        version = validate_database(cn, require_current=False)
        tables = dict(BASE_TABLE_COLUMNS)
        if version >= 1:
            tables['inventory_operations'] = INVENTORY_OPERATION_COLUMNS
        counts = {name: cn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in tables}
        balances = [dict(row) for row in cn.execute('''SELECT p.id AS part_id,p.code,
            COALESCE(SUM(CASE m.movement_type WHEN 'ENTRADA' THEN m.qty_milli
                        WHEN 'SALIDA' THEN -m.qty_milli ELSE 0 END),0) AS stock_milli
            FROM parts p LEFT JOIN movements m ON m.part_id=p.id
            GROUP BY p.id,p.code ORDER BY p.id''')]
        return {'database':str(path), 'schema_version':version,
                'journal_mode':cn.execute('PRAGMA journal_mode').fetchone()[0],
                'sqlite_version':sqlite3.sqlite_version,
                'integrity_check':'ok', 'foreign_key_check':[], 'tables':counts,
                'stock_milli':balances}


def _check_new_destination(destination):
    destination = resolve_database_path(destination)
    if not destination.parent.is_dir():
        raise RuntimeError(f'La carpeta de destino no existe: {destination.parent}. Créala explícitamente.')
    # Reject dangling symlinks and orphan WAL/SHM files as well as existing DBs.
    if os.path.lexists(destination) or any(os.path.lexists(str(destination) + suffix) for suffix in ('-wal', '-shm', '-journal')):
        raise RuntimeError(f'El destino ya existe o tiene archivos SQLite auxiliares: {destination}. '
                           'No se sobrescribe; elige un nombre nuevo.')
    return destination


def _write_snapshot(source, destination, require_current=False):
    destination = _check_new_destination(destination)
    # Same-directory staging and a hard-link publication preserve atomicity on
    # Windows/Linux without the overwrite behavior of rename/replace.
    with TemporaryDirectory(prefix='.gefleet-snapshot-', dir=str(destination.parent)) as folder:
        staging = Path(folder) / 'snapshot.db'
        snapshot = sqlite3.connect(str(staging), isolation_level=None)
        try:
            source.backup(snapshot)
            snapshot.execute('PRAGMA foreign_keys=ON')
            snapshot.execute('PRAGMA journal_mode=DELETE')
            validate_database(snapshot, require_current=require_current)
        finally:
            snapshot.close()
        with staging.open('rb+') as file:
            os.fsync(file.fileno())
        # os.link fails if another process created destination meanwhile. It
        # never replaces a database, and all SQLite handles are already closed.
        _check_new_destination(destination)
        os.link(staging, destination)
    return database_info(destination)


def create_backup(destination, database=None):
    source_path = resolve_database_path(DB if database is None else database)
    destination = _check_new_destination(destination)
    if source_path == destination:
        raise RuntimeError('El respaldo debe guardarse en una ruta diferente de la base original.')
    # SQLite's backup API reads a consistent committed snapshot, including WAL
    # transactions. Never copy just the active database file with read_bytes().
    with _open_database(source_path, mode='ro') as source:
        return _write_snapshot(source, destination)


def restore_backup(source, destination):
    source_path = resolve_database_path(source)
    destination = resolve_database_path(destination)
    if destination == (DATA / 'fleet.db').resolve():
        raise RuntimeError('La restauración no puede reemplazar ni crear data/fleet.db. '
                           'Usa una ubicación nueva y verificada con el servidor detenido.')
    destination = _check_new_destination(destination)
    if not source_path.is_file():
        raise RuntimeError(f'No existe el archivo de respaldo: {source_path}.')
    if any(os.path.lexists(str(source_path) + suffix) for suffix in ('-wal', '-shm', '-journal')):
        raise RuntimeError('El origen tiene archivos SQLite auxiliares. Restaura un respaldo '
                           'independiente creado con /api/backup o db-backup.')
    with source_path.open('rb') as file:
        header = file.read(100)
    if len(header) < 100 or not header.startswith(b'SQLite format 3\x00'):
        raise RuntimeError('El respaldo no es un archivo SQLite válido.')
    if header[18:20] != b'\x01\x01':
        raise RuntimeError('El respaldo usa formato WAL. Genera un respaldo independiente con '
                           '/api/backup o db-backup antes de restaurar.')
    # No schema migration during restoration: version and historical values are
    # preserved exactly; normal startup applies supported migrations afterward.
    with _open_database(source_path, mode='ro') as source_db:
        validate_database(source_db, require_current=False)
        return _write_snapshot(source_db, destination)


def _database_cli():
    parser = argparse.ArgumentParser(description='Gestión local segura de la base Gold Engine Fleet.')
    subparsers = parser.add_subparsers(dest='command', required=True)
    for name in ('db-info', 'db-init', 'db-backup'):
        command = subparsers.add_parser(name)
        command.add_argument('--database', help='Ruta absoluta; prevalece sobre GEFLEET_DB.')
        if name == 'db-backup':
            command.add_argument('--destination', required=True, help='Ruta absoluta de un archivo nuevo.')
    command = subparsers.add_parser('db-restore', help='Restaurar en un archivo NUEVO con el servidor detenido.')
    command.add_argument('--source', required=True, help='Respaldo SQLite independiente.')
    command.add_argument('--destination', required=True, help='Ruta absoluta nueva; nunca se sobrescribe.')
    args = parser.parse_args()
    global DB
    try:
        if getattr(args, 'database', None) is not None:
            DB = resolve_database_path(args.database)
        if args.command == 'db-info':
            result = database_info()
        elif args.command == 'db-init':
            init_db(allow_create=True)
            result = database_info()
        elif args.command == 'db-backup':
            result = create_backup(args.destination)
        else:
            result = restore_backup(args.source, args.destination)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (RuntimeError, sqlite3.Error, OSError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(_database_cli())
