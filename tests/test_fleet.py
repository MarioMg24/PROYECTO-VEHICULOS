import json
import importlib
from pathlib import Path
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient

@pytest.fixture()
def client(monkeypatch,tmp_path):
    import server
    monkeypatch.setattr(server,'DB',tmp_path/'test.db')
    with TestClient(server.app) as c:
        yield c


def vehicle(c,plate='TEST-01'):
    r=c.post('/api/vehicles',json={'plate':plate,'name':'Vehículo de prueba','vehicle_type':'LIVIANO','odometer':1000})
    assert r.status_code==200,r.text
    return next(v['id'] for v in c.get('/api/bootstrap').json()['vehicles'] if v['plate']==plate)


def part(c,code='TEST-F1',unit='unidad'):
    r=c.post('/api/parts',json={'code':code,'name':'Filtro de prueba','unit':unit,'min_qty':'1'})
    assert r.status_code==200,r.text
    return r.json()['id']


def move(c,pid,kind,qty,**kw):
    return c.post('/api/movements',headers={'Idempotency-Key':str(uuid4())},json={'part_id':pid,'movement_type':kind,'quantity':qty,'responsible':'Bodega','reference':'PR-01',**kw})


def test_home_and_manifest(client):
    assert client.get('/').status_code==200
    assert client.get('/service-worker.js').status_code==200
    assert client.get('/manifest.webmanifest').status_code==200
    assert client.get('/api/bootstrap').json()['parts']==[]


def test_vehicles_seed_is_optional(client):
    assert not client.get('/api/bootstrap').json()['vehicles']
    from seed_vehicles import seed
    seed();seed()
    result=client.get('/api/bootstrap').json()['vehicles']
    assert len(result)==9
    assert any(v['plate']=='PFG-2433' for v in result)
    assert client.get('/api/bootstrap').json()['parts']==[]


def test_part_stock_milli_and_prevent_negative(client):
    vid=vehicle(client);pid=part(client)
    assert move(client,pid,'ENTRADA','5').status_code==200
    assert move(client,pid,'SALIDA','7',vehicle_id=vid).status_code==409
    assert move(client,pid,'SALIDA','2',vehicle_id=vid).status_code==200
    p=client.get('/api/bootstrap').json()['parts'][0]
    assert p['stock_milli']==3000
    assert move(client,pid,'ENTRADA','0.5').status_code==422
    assert move(client,pid,'ENTRADA','Infinity').status_code==422
    assert len(client.get('/api/bootstrap').json()['movements'])==2


def test_lubricants_decimal(client):
    pid=part(client,'ACE-01','galón')
    assert move(client,pid,'ENTRADA','880').status_code==200
    assert move(client,pid,'SALIDA','43.125').status_code==200
    assert client.get('/api/bootstrap').json()['parts'][0]['stock_milli']==836875
    assert move(client,pid,'SALIDA','836.876').status_code==409


def test_request_approval_delivery_and_reject_insufficient(client):
    vid=vehicle(client);pid=part(client)
    assert move(client,pid,'ENTRADA','3').status_code==200
    q=client.post('/api/requests',json={'vehicle_id':vid,'requester':'Mecánico','items':[{'part_id':pid,'quantity':'2'}]}).json()['id']
    assert client.get('/api/bootstrap').json()['parts'][0]['stock_milli']==3000
    assert client.post(f'/api/requests/{q}/deliver',headers={'Idempotency-Key':str(uuid4())},json={'received_by':'A','delivered_by':'B','lines':[{'line_id':1,'quantity':'2'}]}).status_code==409
    assert client.post(f'/api/requests/{q}/approve',json={'approved_by':'Jefe'}).status_code==200
    line=client.get(f'/api/requests/{q}').json()['lines'][0]['id']
    assert client.post(f'/api/requests/{q}/deliver',headers={'Idempotency-Key':str(uuid4())},json={'received_by':'A','delivered_by':'B','lines':[{'line_id':line,'quantity':'1'}]}).json()['status']=='PARCIAL'
    assert client.post(f'/api/requests/{q}/deliver',headers={'Idempotency-Key':str(uuid4())},json={'received_by':'A','delivered_by':'B','lines':[{'line_id':line,'quantity':'1'}]}).json()['status']=='ENTREGADA'
    assert client.get('/api/bootstrap').json()['parts'][0]['stock_milli']==1000
    assert client.post(f'/api/requests/{q}/deliver',headers={'Idempotency-Key':str(uuid4())},json={'received_by':'A','delivered_by':'B','lines':[{'line_id':line,'quantity':'1'}]}).status_code==409


def test_inspection_idempotent_and_checklist_only_liviano(client):
    vid=vehicle(client)
    keys=client.get('/api/bootstrap').json()['template']
    answers={f'{i}-{j}':'BIEN' for i,g in enumerate(keys) for j,_ in enumerate(g['items'])}
    payload={'client_uuid':'client-12345678','vehicle_id':vid,'date':'2026-10-12','inspector':'Inspector','odometer':1500,'answers':answers}
    assert client.post('/api/inspections',json={**payload,'answers':{'0-0':'MAL'}}).status_code==422
    r=client.post('/api/inspections',json=payload)
    assert r.status_code==200,r.text
    assert client.post('/api/inspections',json=payload).json()['duplicate']
    assert len(client.get('/api/bootstrap').json()['inspections'])==1
    assert client.get('/api/bootstrap').json()['vehicles'][0]['odometer']==1500
    assert client.get('/api/inspections/1').json()['answers']==answers


def test_maintenance_dates_and_export_backup(client):
    vid=vehicle(client)
    r=client.post('/api/maintenance',json={'vehicle_id':vid,'service_name':'Cambio de aceite','performed_on':'2026-10-12','odometer':1500,'interval_km':5000})
    assert r.status_code==200,r.text
    assert client.get('/api/bootstrap').json()['maintenance'][0]['next_due_km']==6500
    assert 'Vehículo'.encode() in client.get('/api/export/vehicles').content
    assert b'SQLite format 3' in client.get('/api/backup').content[:30]
