"""Technical maintenance history regressions; every database is temporary."""
from contextlib import closing
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient

import server


NEW_FIELDS = ('maintenance_type', 'work_performed', 'responsible', 'location')


@pytest.fixture
def history_client(monkeypatch, tmp_path):
    path = tmp_path / 'technical-history.db'
    monkeypatch.setattr(server, 'DB', path)
    monkeypatch.setenv('GEFLEET_DB', str(path))
    monkeypatch.setenv('GEFLEET_ALLOW_CREATE_DB', '1')
    with TestClient(server.app) as client:
        yield client, path


def vehicle(client, **changes):
    payload = {
        'plate': 'PDX-2511', 'name': 'Chevrolet NLR', 'vehicle_type': 'PESADO',
        'brand': 'Chevrolet', 'model': 'NLR', 'odometer': 118539, **changes,
    }
    response = client.post('/api/vehicles', json=payload)
    assert response.status_code == 200, response.text
    return next(v for v in client.get('/api/bootstrap').json()['vehicles']
                if v['plate'] == payload['plate'])


def service_payload(target_vehicle_id, **changes):
    return {
        'vehicle_id': target_vehicle_id, 'service_name': 'Cambio de aceite y filtros',
        'performed_on': '2026-09-30', 'odometer': 118539, 'interval_km': 4500,
        'maintenance_type': 'MPG', 'work_performed': 'Cambio de aceite y filtros',
        'responsible': 'Samir Lobato', 'location': 'Base Coca',
        'notes': 'Servicio completado; revisar niveles antes de salir.', **changes,
    }


def add_service(client, vehicle_id, **changes):
    response = client.post('/api/maintenance', json=service_payload(vehicle_id, **changes))
    assert response.status_code == 200, response.text
    return response.json()['id']


def history(client, **params):
    response = client.get('/api/maintenance', params=params)
    assert response.status_code == 200, response.text
    return response.json()


def detail(client, service_id):
    response = client.get(f'/api/maintenance/{service_id}')
    assert response.status_code == 200, response.text
    return response.json()


def business_snapshot(path):
    """Project legacy columns explicitly, so added fields cannot hide data loss."""
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as cn:
        tables = dict(server.BASE_TABLE_COLUMNS)
        existing = {row[0] for row in cn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'inventory_operations' in existing:
            tables['inventory_operations'] = server.INVENTORY_OPERATION_COLUMNS
        return {
            table: cn.execute(f'SELECT {",".join(columns.split())} FROM "{table}" ORDER BY rowid').fetchall()
            for table, columns in tables.items()
        }


def old_database(path, version):
    """Build genuine v0/v1 schema and relationships without current initialization."""
    with closing(sqlite3.connect(path, isolation_level=None)) as cn:
        cn.execute('PRAGMA foreign_keys=ON')
        cn.executescript(server.BASE_SCHEMA)
        if version == 1:
            server._migration_1(cn)
        cn.execute(f'PRAGMA user_version={version}')
        cn.execute("""INSERT INTO vehicles(id,plate,name,vehicle_type,brand,model,odometer,notes)
            VALUES(1,'PDX-2511','Chevrolet NLR','PESADO','Chevrolet','NLR',118539,'Nota original')""")
        cn.execute("""INSERT INTO inspections(id,client_uuid,vehicle_id,date,inspector,odometer,
            answers_json,observations) VALUES(1,'old-inspection',1,'2026-09-29','Conductor',118500,?,?)""",
            (json.dumps({'0-0': 'BIEN', '0-1': 'MAL', '0-2': 'N/A'}), 'Observación sin cambios'))
        cn.execute("""INSERT INTO maintenance(id,vehicle_id,service_name,performed_on,odometer,
            interval_km,next_due_km,notes) VALUES(1,1,'Cambio de aceite','2026-09-30',118539,
            4500,123039,'Registro antiguo completo')""")
        cn.execute("INSERT INTO parts(id,code,name,unit,min_milli) VALUES(1,'OLD-OIL','Aceite','galón',2000)")
        cn.execute("INSERT INTO compatibility VALUES(1,1,'Compatible con NLR')")
        cn.execute("""INSERT INTO movements(id,part_id,movement_type,qty_milli,responsible,notes)
            VALUES(1,1,'ENTRADA',10000,'Bodega','Compra original')""")
        cn.execute("""INSERT INTO movements(id,part_id,movement_type,qty_milli,vehicle_id,reference,responsible)
            VALUES(2,1,'SALIDA',1750,1,'SOL-00001','Bodega')""")
        cn.execute("""INSERT INTO requests(id,vehicle_id,requester,reason,status,approved_by,received_by)
            VALUES(1,1,'Samir','Aceite','PARCIAL','Jefe','Conductor')""")
        cn.execute('INSERT INTO request_lines VALUES(1,1,1,4000,1750)')
        if version == 1:
            cn.execute("""INSERT INTO inventory_operations(operation_key,scope,request_hash,response_json)
                VALUES('old-partial-delivery','delivery:1','preserve-hash',?)""",
                ('{"ok":true,"status":"PARCIAL","delivered_milli":1750}',))
    return path


def assert_database_valid(path, version):
    with closing(sqlite3.connect(path)) as cn:
        assert cn.execute('PRAGMA user_version').fetchone()[0] == version
        assert cn.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert cn.execute('PRAGMA foreign_key_check').fetchall() == []
        assert server.stock(cn, 1) == 8250


def test_required_chevrolet_example_is_available_in_full_history_and_detail(history_client):
    client, path = history_client
    truck = vehicle(client)
    service_id = add_service(client, truck['id'])
    result = history(client, vehicle_id=truck['id'])
    saved = detail(client, service_id)
    expected = {
        **service_payload(truck['id']), 'id': service_id, 'next_due_km': 123039,
        'vehicle_name': 'Chevrolet NLR', 'plate': 'PDX-2511',
    }
    for field, value in expected.items():
        assert saved[field] == value
        assert result['items'][0][field] == value
    assert result['total'] == 1
    assert result['latest']['id'] == service_id
    assert result['plans'][0]['next_due_km'] == 123039
    assert result['plans'][0]['next_due_km'] - truck['odometer'] == 4500
    with closing(sqlite3.connect(path)) as cn:
        row = cn.execute('SELECT maintenance_type,work_performed,responsible,location FROM maintenance').fetchone()
    assert row == ('MPG', 'Cambio de aceite y filtros', 'Samir Lobato', 'Base Coca')


def test_existing_maintenance_client_remains_compatible(history_client):
    client, _ = history_client
    truck = vehicle(client)
    response = client.post('/api/maintenance', json={
        'vehicle_id': truck['id'], 'service_name': 'Cambio de aceite',
        'performed_on': '2026-09-30', 'odometer': 118539, 'interval_km': 4500,
        'notes': 'Registro del cliente anterior',
    })
    assert response.status_code == 200, response.text
    saved = detail(client, response.json()['id'])
    assert all(saved[field] == '' for field in NEW_FIELDS)
    assert saved['notes'] == 'Registro del cliente anterior'
    assert saved['next_due_km'] == 123039
    assert history(client, maintenance_type='LEGACY')['items'][0]['id'] == saved['id']


def test_history_scopes_records_latest_and_plans_to_one_vehicle(history_client):
    client, _ = history_client
    truck = vehicle(client)
    other = vehicle(client, plate='OTHER-01', name='Otro vehículo')
    wanted = add_service(client, truck['id'])
    add_service(client, other['id'], performed_on='2026-10-12')
    scoped = history(client, vehicle_id=truck['id'])
    assert [row['id'] for row in scoped['items']] == [wanted]
    assert scoped['latest']['id'] == wanted
    assert [plan['vehicle_id'] for plan in scoped['plans']] == [truck['id']]
    assert history(client)['total'] == 2


def test_date_and_type_filters_are_inclusive_and_do_not_change_current_summary(history_client):
    client, _ = history_client
    truck = vehicle(client)
    older = add_service(client, truck['id'], performed_on='2026-09-01', odometer=115000)
    current = add_service(client, truck['id'])
    corrective = add_service(client, truck['id'], maintenance_type='CORRECTIVO',
        performed_on='2026-10-02', interval_km=None, service_name='Reparación de frenos')
    legacy = add_service(client, truck['id'], maintenance_type='',
        performed_on='2026-08-01', interval_km=None)
    result = history(client, vehicle_id=truck['id'], date_from='2026-09-01',
        date_to='2026-09-30', maintenance_type='MPG')
    assert [row['id'] for row in result['items']] == [current, older]
    assert result['total'] == 2
    assert result['latest']['id'] == corrective
    assert result['plans'][0]['id'] == current
    assert history(client, maintenance_type='CORRECTIVO')['items'][0]['id'] == corrective
    assert history(client, maintenance_type='LEGACY')['items'][0]['id'] == legacy
    empty = history(client, vehicle_id=truck['id'], date_from='2027-01-01')
    assert empty['items'] == [] and empty['total'] == 0
    assert empty['latest']['id'] == corrective
    assert empty['plans'][0]['id'] == current


def test_effective_date_and_stable_ties_determine_order_and_current_plan(history_client):
    client, _ = history_client
    truck = vehicle(client)
    current = add_service(client, truck['id'], performed_on='2026-10-12', odometer=120000)
    historical = add_service(client, truck['id'], performed_on='2026-01-01', odometer=100000)
    lower_same_date = add_service(client, truck['id'], performed_on='2026-10-12', odometer=119000)
    tied = add_service(client, truck['id'], performed_on='2026-10-12', odometer=120000)
    result = history(client, vehicle_id=truck['id'])
    assert [row['id'] for row in result['items']] == [tied, current, lower_same_date, historical]
    assert result['latest']['id'] == tied
    assert result['plans'][0]['id'] == tied
    assert result['plans'][0]['next_due_km'] == 124500
    saved_vehicle = next(v for v in client.get('/api/bootstrap').json()['vehicles'] if v['id'] == truck['id'])
    assert saved_vehicle['odometer'] == 120000


def test_odometer_correction_remains_in_history_but_is_not_the_last_technical_service(history_client):
    client, _ = history_client
    truck = vehicle(client)
    service_id = add_service(client, truck['id'])
    response = client.post(f"/api/vehicles/{truck['id']}/odometer-correction", json={
        'odometer': 118500, 'expected_odometer': 118539, 'reason': 'Lectura corregida documentada',
        'responsible': 'Supervisor', 'corrected_on': '2026-10-12',
    })
    assert response.status_code == 200, response.text
    correction_id = response.json()['id']
    result = history(client, vehicle_id=truck['id'])
    assert result['items'][0]['id'] == correction_id
    assert result['latest']['id'] == service_id
    assert result['plans'][0]['id'] == service_id
    assert result['plans'][0]['next_due_km'] == 123039
    assert detail(client, correction_id)['interval_km'] is None


def test_independent_services_and_corrective_work_preserve_preventive_plans(history_client):
    client, _ = history_client
    truck = vehicle(client)
    oil = add_service(client, truck['id'], service_name='Cambio de aceite', interval_km=4500)
    brakes = add_service(client, truck['id'], service_name='Revisión de frenos', interval_km=10000)
    repair = add_service(client, truck['id'], service_name='Cambio de aceite',
        maintenance_type='CORRECTIVO', interval_km=None, performed_on='2026-10-12', odometer=122539)
    plans = {plan['service_key']: plan for plan in history(client, vehicle_id=truck['id'])['plans']}
    assert set(plans) == {'cambio de aceite', 'revisión de frenos'}
    assert plans['cambio de aceite']['id'] == oil
    assert plans['revisión de frenos']['id'] == brakes
    assert plans['cambio de aceite']['next_due_km'] == 123039
    assert plans['revisión de frenos']['next_due_km'] == 128539
    assert history(client, vehicle_id=truck['id'])['latest']['id'] == repair
    snapshot = client.get('/api/bootstrap').json()
    reading = next(v['odometer'] for v in snapshot['vehicles'] if v['id'] == truck['id'])
    assert plans['cambio de aceite']['next_due_km'] - reading == 500
    assert {plan['id'] for plan in snapshot['maintenance_plans']} == {oil, brakes}


def test_history_plan_includes_current_reading_after_another_session_updates_vehicle(history_client):
    client, _ = history_client
    truck = vehicle(client)
    current = add_service(client, truck['id'])
    old_snapshot = client.get('/api/bootstrap').json()
    old_plan = old_snapshot['maintenance_plans'][0]
    assert old_plan['id'] == current and old_plan['next_due_km'] == 123039
    assert 'current_odometer' not in old_plan
    response = client.patch(f"/api/vehicles/{truck['id']}", json={'odometer': 122539})
    assert response.status_code == 200, response.text
    fresh_plan = history(client, vehicle_id=truck['id'])['plans'][0]
    assert fresh_plan['id'] == current
    assert fresh_plan['next_due_km'] == 123039
    assert fresh_plan['current_odometer'] == 122539
    assert fresh_plan['next_due_km'] - fresh_plan['current_odometer'] == 500
    assert old_snapshot['vehicles'][0]['odometer'] == 118539
    fresh_bootstrap_plan = client.get('/api/bootstrap').json()['maintenance_plans'][0]
    assert fresh_bootstrap_plan == old_plan


def test_preventive_work_without_interval_does_not_invent_a_plan(history_client):
    client, _ = history_client
    truck = vehicle(client)
    service_id = add_service(client, truck['id'], interval_km=None)
    result = history(client, vehicle_id=truck['id'])
    assert result['latest']['id'] == service_id
    assert result['plans'] == []
    assert detail(client, service_id)['next_due_km'] is None


def test_next_service_is_derived_from_interval_even_if_client_supplies_another_target(history_client):
    client, _ = history_client
    truck = vehicle(client)
    service_id = add_service(client, truck['id'], interval_km=7500, next_due_km=123039)
    assert detail(client, service_id)['next_due_km'] == 126039
    assert history(client, vehicle_id=truck['id'])['plans'][0]['next_due_km'] == 126039


def test_invalid_legacy_date_remains_visible_without_replacing_latest_or_plan(history_client):
    client, path = history_client
    truck = vehicle(client)
    current = add_service(client, truck['id'])
    with closing(sqlite3.connect(path)) as cn:
        cn.execute('''INSERT INTO maintenance(vehicle_id,service_name,performed_on,odometer,
            interval_km,next_due_km,notes) VALUES(?,?,?,120000,4500,124500,?)''',
            (truck['id'], 'Cambio de aceite y filtros', '2099-02-30', 'Fecha antigua inválida; conservar'))
        bad_id = cn.execute('SELECT last_insert_rowid()').fetchone()[0]
        cn.commit()
    result = history(client, vehicle_id=truck['id'])
    assert {row['id'] for row in result['items']} == {current, bad_id}
    assert detail(client, bad_id)['performed_on'] == '2099-02-30'
    assert result['latest']['id'] == current
    assert result['plans'][0]['id'] == current


def test_corrective_row_with_old_interval_never_resets_preventive_plan(history_client):
    client, path = history_client
    truck = vehicle(client)
    current = add_service(client, truck['id'])
    with closing(sqlite3.connect(path)) as cn:
        cn.execute('''INSERT INTO maintenance(vehicle_id,service_name,performed_on,odometer,
            interval_km,next_due_km,maintenance_type) VALUES(?,?,?,120000,10000,130000,'CORRECTIVO')''',
            (truck['id'], 'Cambio de aceite y filtros', '2026-10-12'))
        correction = cn.execute('SELECT last_insert_rowid()').fetchone()[0]
        cn.commit()
    result = history(client, vehicle_id=truck['id'])
    assert result['latest']['id'] == correction
    assert [plan['id'] for plan in result['plans']] == [current]
    assert result['plans'][0]['next_due_km'] == 123039


def test_all_history_is_accessible_above_200_records_with_stable_pagination(history_client):
    client, path = history_client
    truck = vehicle(client)
    current = add_service(client, truck['id'])
    with closing(sqlite3.connect(path)) as cn:
        cn.executemany('''INSERT INTO maintenance(vehicle_id,service_name,performed_on,odometer,
            interval_km,next_due_km,notes,maintenance_type,work_performed,responsible,location)
            VALUES(?,?,?,?,NULL,NULL,?,'CORRECTIVO',?,'Mecánico','Base')''', [
            (truck['id'], 'Reparación histórica', (date(2025, 1, 1) + timedelta(days=i)).isoformat(),
                100000 + i, f'Histórico {i}', f'Trabajo {i}') for i in range(236)
        ])
        cn.commit()
    pages = [history(client, vehicle_id=truck['id'], page=page, page_size=100) for page in (1, 2, 3)]
    assert all(result['total'] == 237 and result['pages'] == 3 for result in pages)
    assert [len(result['items']) for result in pages] == [100, 100, 37]
    assert [result['page'] for result in pages] == [1, 2, 3]
    ids = [item['id'] for result in pages for item in result['items']]
    assert len(ids) == len(set(ids)) == 237
    assert ids[0] == current
    assert detail(client, ids[-1])['notes'] == 'Histórico 0'
    assert all(result['latest']['id'] == current for result in pages)
    assert all(result['plans'][0]['id'] == current for result in pages)
    beyond = history(client, vehicle_id=truck['id'], page=4, page_size=100)
    assert beyond['items'] == [] and beyond['total'] == 237
    assert len(client.get('/api/bootstrap').json()['maintenance']) <= 200


@pytest.mark.parametrize('params', [
    {'page': 0}, {'page': -1}, {'page': '1.5'}, {'page_size': 0}, {'page_size': 101},
    {'vehicle_id': 0}, {'vehicle_id': -1}, {'vehicle_id': 'invalid'},
    {'vehicle_id': 9007199254740992}, {'page': 2147483648},
    {'date_from': '2026-02-30'}, {'date_to': '2026-13-01'}, {'date_from': '30/09/2026'},
    {'date_from': '2026-10-01', 'date_to': '2026-09-30'},
    {'maintenance_type': 'OTHER'}, {'maintenance_type': "MPG' OR 1=1 --"},
])
def test_history_rejects_invalid_filters_without_server_errors(history_client, params):
    client, _ = history_client
    response = client.get('/api/maintenance', params=params)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize('changes', [
    {'performed_on': '2026-02-30'}, {'performed_on': '30/09/2026'}, {'performed_on': '2026-13-01'},
    {'odometer': -1}, {'odometer': 10000001}, {'odometer': 1.5}, {'odometer': True},
    {'interval_km': 0}, {'interval_km': -4500}, {'interval_km': 1000001},
    {'maintenance_type': 'OTHER'}, {'maintenance_type': 'CORRECTIVO'},
    {'service_name': 'x' * 251}, {'work_performed': 'x' * 4001}, {'notes': 'x' * 4001},
    {'responsible': 'x' * 151}, {'location': 'x' * 151}, {'responsible': None},
    {'vehicle_id': 0}, {'vehicle_id': -1}, {'vehicle_id': 9007199254740992},
])
def test_registration_validates_dates_readings_types_and_text_without_partial_writes(history_client, changes):
    client, _ = history_client
    truck = vehicle(client)
    response = client.post('/api/maintenance', json=service_payload(truck['id'], **changes))
    assert response.status_code == 422, response.text
    assert history(client)['total'] == 0
    assert client.get('/api/bootstrap').json()['vehicles'][0]['odometer'] == 118539


def test_text_at_limits_is_preserved_and_not_silently_truncated(history_client):
    client, _ = history_client
    truck = vehicle(client)
    changes = {
        'service_name': 's' * 250, 'work_performed': 'á' * 4000, 'notes': 'ñ' * 4000,
        'responsible': 'r' * 150, 'location': 'l' * 150,
    }
    service_id = add_service(client, truck['id'], **changes)
    saved = detail(client, service_id)
    for field, value in changes.items():
        assert saved[field] == value


def test_missing_vehicle_or_service_returns_404_without_creating_records(history_client):
    client, _ = history_client
    assert client.get('/api/maintenance/999999').status_code == 404
    assert client.get('/api/maintenance', params={'vehicle_id': 999999}).status_code == 404
    response = client.post('/api/maintenance', json=service_payload(999999))
    assert response.status_code == 404, response.text
    assert history(client)['total'] == 0


@pytest.mark.parametrize('service_id', ['0', '-1', '9007199254740992', 'invalid'])
def test_detail_rejects_invalid_identifiers_without_server_errors(history_client, service_id):
    client, _ = history_client
    assert client.get(f'/api/maintenance/{service_id}').status_code == 422


@pytest.mark.parametrize('old_version', [0, 1])
def test_migrate_old_copy_preserves_every_original_value_and_adds_empty_fields(monkeypatch, tmp_path, old_version):
    original = old_database(tmp_path / 'old.db', old_version)
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    copy = tmp_path / 'copy-to-migrate.db'
    shutil.copyfile(original, copy)
    before = business_snapshot(copy)
    monkeypatch.setattr(server, 'DB', copy)
    server.init_db(allow_create=False)
    after = business_snapshot(copy)
    assert_database_valid(copy, 2)
    for table, values in before.items():
        assert after[table] == values
    assert hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
    with closing(sqlite3.connect(copy)) as cn:
        row = cn.execute('SELECT maintenance_type,work_performed,responsible,location FROM maintenance').fetchone()
    assert row == ('', '', '', '')
    if old_version == 0:
        assert after['inventory_operations'] == []
    else:
        assert after['inventory_operations'][0][0] == 'old-partial-delivery'


def test_migration_repeats_without_duplicate_fields_rows_or_indexes(monkeypatch, tmp_path):
    path = old_database(tmp_path / 'repeat-v2.db', 1)
    monkeypatch.setattr(server, 'DB', path)
    server.init_db(allow_create=False)
    before = business_snapshot(path)
    with closing(sqlite3.connect(path)) as cn:
        schema_before = cn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall()
    server.init_db(allow_create=False)
    server.init_db(allow_create=False)
    with closing(sqlite3.connect(path)) as cn:
        assert cn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall() == schema_before
        columns = [row[1] for row in cn.execute('PRAGMA table_info(maintenance)')]
        assert all(columns.count(name) == 1 for name in NEW_FIELDS)
    assert business_snapshot(path) == before
    assert_database_valid(path, 2)


def test_failure_after_new_columns_rolls_back_migration_and_all_data(monkeypatch, tmp_path):
    path = old_database(tmp_path / 'rollback-v2.db', 1)
    before = business_snapshot(path)
    real_migration = server.MIGRATIONS[2]

    def fail_after_schema(cn):
        real_migration(cn)
        cn.execute("UPDATE maintenance SET notes='Must not persist',responsible='Unexpected'")
        raise RuntimeError('Simulated v2 migration failure')

    monkeypatch.setattr(server, 'DB', path)
    monkeypatch.setattr(server, 'MIGRATIONS', {**server.MIGRATIONS, 2: fail_after_schema})
    with pytest.raises(RuntimeError, match='Simulated v2 migration failure'):
        server.init_db(allow_create=False)
    assert business_snapshot(path) == before
    assert_database_valid(path, 1)
    with closing(sqlite3.connect(path)) as cn:
        columns = {row[1] for row in cn.execute('PRAGMA table_info(maintenance)')}
    assert not columns.intersection(NEW_FIELDS)


def test_migrated_legacy_record_is_still_consultable_and_can_be_filtered(monkeypatch, tmp_path):
    path = old_database(tmp_path / 'old-history.db', 1)
    monkeypatch.setattr(server, 'DB', path)
    monkeypatch.setenv('GEFLEET_DB', str(path))
    with TestClient(server.app) as client:
        result = history(client, vehicle_id=1, maintenance_type='LEGACY')
        assert result['total'] == 1
        assert result['items'][0]['notes'] == 'Registro antiguo completo'
        assert result['items'][0]['service_name'] == 'Cambio de aceite'
        assert all(result['items'][0][name] == '' for name in NEW_FIELDS)
        assert result['plans'][0]['next_due_km'] == 123039
        assert detail(client, 1)['plate'] == 'PDX-2511'


def test_backups_before_and_after_migration_restore_compatible_history_and_inventory(monkeypatch, tmp_path):
    path = old_database(tmp_path / 'source-v1.db', 1)
    old_values = business_snapshot(path)
    before_backup = tmp_path / 'before-v2.db'
    server.create_backup(before_backup, database=path)
    before_restore = tmp_path / 'restore-old.db'
    server.restore_backup(before_backup, before_restore)
    assert_database_valid(before_restore, 1)
    assert business_snapshot(before_restore) == old_values
    monkeypatch.setattr(server, 'DB', before_restore)
    server.init_db(allow_create=False)
    with TestClient(server.app) as client:
        created = add_service(client, 1)
        assert history(client, vehicle_id=1)['total'] == 2
        assert detail(client, created)['responsible'] == 'Samir Lobato'
        response = client.get('/api/backup')
        assert response.status_code == 200, response.text
    after_backup = tmp_path / 'after-v2.db'
    after_backup.write_bytes(response.content)
    after_restore = tmp_path / 'restore-new.db'
    server.restore_backup(after_backup, after_restore)
    assert_database_valid(after_restore, 2)
    assert business_snapshot(after_restore) == business_snapshot(before_restore)
    with closing(sqlite3.connect(after_restore)) as cn:
        assert cn.execute('SELECT responsible FROM maintenance WHERE id=?', (created,)).fetchone()[0] == 'Samir Lobato'
        assert cn.execute('SELECT operation_key FROM inventory_operations').fetchall() == [('old-partial-delivery',)]
