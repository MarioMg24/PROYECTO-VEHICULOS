"""Odometer and maintenance regressions; all writes use temporary databases."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import subprocess
from threading import Barrier

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def mileage_client(monkeypatch, tmp_path):
    import server

    monkeypatch.setenv('GEFLEET_DB', str(tmp_path / 'mileage.db'))
    monkeypatch.setattr(server, 'DB', tmp_path / 'mileage.db')
    with TestClient(server.app) as client:
        yield client


def create_vehicle(client, **changes):
    payload = {
        'plate': 'KM-001', 'name': 'Temporary vehicle',
        'vehicle_type': 'LIVIANO', 'brand': 'Toyota', 'model': 'Hilux',
        'odometer': 118539, 'notes': 'Preserve this note', **changes,
    }
    response = client.post('/api/vehicles', json=payload)
    assert response.status_code == 200, response.text
    return next(v for v in client.get('/api/bootstrap').json()['vehicles']
                if v['plate'] == payload['plate'])


def create_service(client, vehicle_id, **changes):
    response = client.post('/api/maintenance', json={
        'vehicle_id': vehicle_id, 'service_name': 'Cambio de aceite',
        'performed_on': '2026-10-12', 'odometer': 118539,
        'interval_km': 4500, **changes,
    })
    assert response.status_code == 200, response.text
    return response.json()['id']


def read_vehicle(client, vehicle_id):
    return next(v for v in client.get('/api/bootstrap').json()['vehicles']
                if v['id'] == vehicle_id)


def frontend_eval(snapshot, expression='latestDue()'):
    """Run the real frontend functions against API state without browser side effects."""
    app_path = Path(__file__).resolve().parents[1] / 'static' / 'app.js'
    script = """const fs = require('node:fs');
const vm = require('node:vm');
const data = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = fs.readFileSync(data.path, 'utf8').split('function renderParts()')[0];
const context = vm.createContext({console});
vm.runInContext(source, context);
vm.runInContext('state = '+JSON.stringify(data.snapshot), context);
process.stdout.write(JSON.stringify(vm.runInContext(data.expression, context)));
"""
    result = subprocess.run(['node', '-e', script], input=json.dumps({
        'path': str(app_path), 'snapshot': snapshot, 'expression': expression,
    }), capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def frontend_due(snapshot):
    return frontend_eval(snapshot)


def test_edit_name_preserves_odometer_and_other_fields(mileage_client):
    client = mileage_client
    before = create_vehicle(client)
    response = client.patch(f"/api/vehicles/{before['id']}", json={'name': 'Renamed vehicle'})
    assert response.status_code == 200, response.text
    after = read_vehicle(client, before['id'])
    assert after == {**before, 'name': 'Renamed vehicle'}


def test_edit_plate_preserves_other_fields(mileage_client):
    client = mileage_client
    before = create_vehicle(client)
    response = client.patch(f"/api/vehicles/{before['id']}", json={'plate': 'KM-002'})
    assert response.status_code == 200, response.text
    assert read_vehicle(client, before['id']) == {**before, 'plate': 'KM-002'}


def test_normal_edit_rejects_odometer_reduction_without_changes(mileage_client):
    client = mileage_client
    before = create_vehicle(client)
    response = client.patch(f"/api/vehicles/{before['id']}", json={
        key: (118538 if key == 'odometer' else before[key])
        for key in ('plate', 'name', 'vehicle_type', 'brand', 'model', 'odometer', 'notes')
    })
    assert response.status_code == 409, response.text
    assert read_vehicle(client, before['id']) == before


def test_historical_service_does_not_replace_current_plan(mileage_client):
    client = mileage_client
    vehicle = create_vehicle(client)
    current_id = create_service(client, vehicle['id'])
    create_service(client, vehicle['id'], performed_on='2025-04-01', odometer=100000)
    due = frontend_due(client.get('/api/bootstrap').json())
    assert len(due) == 1
    assert due[0]['id'] == current_id
    assert due[0]['next_due_km'] == 123039
    assert due[0]['kmLeft'] == 4500
    assert read_vehicle(client, vehicle['id'])['odometer'] == 118539


def test_plans_remain_available_after_200_global_history_records(mileage_client):
    client = mileage_client
    vehicle = create_vehicle(client)
    current_id = create_service(client, vehicle['id'])
    other = create_vehicle(client, plate='KM-OTHER')
    for _ in range(205):
        create_service(client, other['id'], service_name='Reparacion correctiva', interval_km=None)
    snapshot = client.get('/api/bootstrap').json()
    due = frontend_due(snapshot)
    assert any(plan['id'] == current_id for plan in due), due
    assert len(snapshot['maintenance']) <= 200


def test_plate_and_name_do_not_reset_omitted_fields(mileage_client):
    client = mileage_client
    before = create_vehicle(client)
    response = client.patch(f"/api/vehicles/{before['id']}", json={
        'plate': 'KM-CHANGED', 'name': 'Changed vehicle',
    })
    assert response.status_code == 200, response.text
    assert read_vehicle(client, before['id']) == {
        **before, 'plate': 'KM-CHANGED', 'name': 'Changed vehicle',
    }


def test_stale_vehicle_edit_rejected_even_if_proposed_reading_is_higher(mileage_client):
    client = mileage_client
    vehicle = create_vehicle(client)
    create_service(client, vehicle['id'], odometer=120000)
    before = read_vehicle(client, vehicle['id'])
    response = client.patch(f"/api/vehicles/{vehicle['id']}", json={
        'name': 'Stale edit', 'odometer': 121000,
        'expected_odometer': vehicle['odometer'],
    })
    assert response.status_code == 409, response.text
    assert read_vehicle(client, vehicle['id']) == before


def test_partial_metadata_edit_does_not_overwrite_a_new_reading(mileage_client):
    client = mileage_client
    vehicle = create_vehicle(client)
    create_service(client, vehicle['id'], odometer=120000)
    response = client.patch(f"/api/vehicles/{vehicle['id']}", json={'brand': 'Updated brand'})
    assert response.status_code == 200, response.text
    after = read_vehicle(client, vehicle['id'])
    assert after['odometer'] == 120000
    assert after['brand'] == 'Updated brand'
    assert after['model'] == vehicle['model']


def test_concurrent_vehicle_edits_cannot_overwrite_newer_odometer(mileage_client):
    client = mileage_client
    vehicle = create_vehicle(client)
    barrier = Barrier(2)

    def update(odometer):
        barrier.wait()
        return client.patch(f"/api/vehicles/{vehicle['id']}", json={
            'odometer': odometer, 'expected_odometer': vehicle['odometer'],
        })

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(update, [120000, 121000]))
    assert sorted(response.status_code for response in responses) == [200, 409]
    successful_odometer = [120000, 121000][next(
        i for i, response in enumerate(responses) if response.status_code == 200)]
    assert read_vehicle(client, vehicle['id'])['odometer'] == successful_odometer


def test_duplicate_plate_rolls_back_entire_vehicle_edit(mileage_client):
    client = mileage_client
    vehicle = create_vehicle(client)
    create_vehicle(client, plate='KM-OTHER')
    response = client.patch(f"/api/vehicles/{vehicle['id']}", json={
        'plate': 'KM-OTHER', 'odometer': 120000, 'name': 'Must not change',
    })
    assert response.status_code == 409, response.text
    assert read_vehicle(client, vehicle['id']) == vehicle


@pytest.mark.parametrize('field', ['plate', 'name', 'vehicle_type', 'brand', 'model', 'odometer', 'notes'])
def test_patch_rejects_explicit_null_without_erasing_data(mileage_client, field):
    client = mileage_client
    vehicle = create_vehicle(client)
    response = client.patch(f"/api/vehicles/{vehicle['id']}", json={field: None})
    assert response.status_code == 422, response.text
    assert read_vehicle(client, vehicle['id']) == vehicle


@pytest.mark.parametrize('invalid', [-1, 10000001, 2**63, 1.5, True])
def test_invalid_odometer_rejected_without_http_500(mileage_client, invalid):
    client = mileage_client
    vehicle = create_vehicle(client)
    assert client.post('/api/vehicles', json={
        'plate': 'INVALID-KM', 'name': 'Invalid', 'odometer': invalid,
    }).status_code == 422
    assert client.patch(f"/api/vehicles/{vehicle['id']}", json={
        'odometer': invalid,
    }).status_code == 422
    assert client.post('/api/maintenance', json={
        'vehicle_id': vehicle['id'], 'service_name': 'Invalid mileage',
        'performed_on': '2026-10-12', 'odometer': invalid, 'interval_km': 4500,
    }).status_code == 422
    snapshot = client.get('/api/bootstrap').json()
    assert len(snapshot['vehicles']) == 1
    assert snapshot['maintenance'] == []
    assert read_vehicle(client, vehicle['id']) == vehicle


@pytest.mark.parametrize('invalid', [0, -1, 1000001, 2**63, 1.5, True])
def test_invalid_intervals_rejected_without_writes(mileage_client, invalid):
    client = mileage_client
    vehicle = create_vehicle(client)
    response = client.post('/api/maintenance', json={
        'vehicle_id': vehicle['id'], 'service_name': 'Invalid interval',
        'performed_on': '2026-10-12', 'odometer': 120000, 'interval_km': invalid,
    })
    assert response.status_code == 422, response.text
    assert read_vehicle(client, vehicle['id']) == vehicle
    assert client.get('/api/bootstrap').json()['maintenance'] == []


@pytest.mark.parametrize('invalid', [
    '2026-02-30', '2025-02-29', '2026-13-01', '2026-00-10',
    'fecha inexistente', '2026-1-1', '20261012', '', None,
])
def test_invalid_service_dates_rejected_without_writes(mileage_client, invalid):
    client = mileage_client
    vehicle = create_vehicle(client)
    response = client.post('/api/maintenance', json={
        'vehicle_id': vehicle['id'], 'service_name': 'Invalid date',
        'performed_on': invalid, 'odometer': 120000, 'interval_km': 4500,
    })
    assert response.status_code == 422, response.text
    assert read_vehicle(client, vehicle['id']) == vehicle
    assert client.get('/api/bootstrap').json()['maintenance'] == []


def test_leap_date_and_upper_limits_are_valid(mileage_client):
    client = mileage_client
    vehicle = create_vehicle(client, odometer=10000000)
    create_service(client, vehicle['id'], performed_on='2024-02-29',
                   odometer=10000000, interval_km=1000000)
    due = frontend_due(client.get('/api/bootstrap').json())
    assert due[0]['next_due_km'] == 11000000
    assert due[0]['kmLeft'] == 1000000


def test_newer_effective_service_changes_plan_and_uses_its_interval(mileage_client):
    client = mileage_client
    vehicle = create_vehicle(client)
    create_service(client, vehicle['id'])
    newer = create_service(client, vehicle['id'], performed_on='2026-10-13',
                           odometer=120000, interval_km=8000)
    due = frontend_due(client.get('/api/bootstrap').json())
    assert len(due) == 1
    assert due[0]['id'] == newer
    assert due[0]['next_due_km'] == 128000
    assert due[0]['kmLeft'] == 8000
    assert read_vehicle(client, vehicle['id'])['odometer'] == 120000


def test_different_service_types_keep_independent_intervals_and_plans(mileage_client):
    client = mileage_client
    vehicle = create_vehicle(client)
    oil = create_service(client, vehicle['id'])
    brakes = create_service(client, vehicle['id'], service_name='Revision de frenos', interval_km=10000)
    snapshot = client.get('/api/bootstrap').json()
    assert {plan['id'] for plan in snapshot['maintenance_plans']} == {oil, brakes}
    due = {plan['id']: plan for plan in frontend_due(snapshot)}
    assert due[oil]['next_due_km'] == 123039
    assert due[oil]['kmLeft'] == 4500
    assert due[brakes]['next_due_km'] == 128539
    assert due[brakes]['kmLeft'] == 10000


def test_corrective_services_do_not_clear_or_reset_preventive_plans(mileage_client):
    client = mileage_client
    vehicle = create_vehicle(client)
    oil = create_service(client, vehicle['id'])
    brakes = create_service(client, vehicle['id'], service_name='Revision de frenos', interval_km=10000)
    create_service(client, vehicle['id'], interval_km=None, performed_on='2026-10-13', odometer=120000)
    create_service(client, vehicle['id'], service_name='Cambio de bomba', interval_km=None,
                   performed_on='2026-10-13', odometer=120001)
    due = {plan['id']: plan for plan in frontend_due(client.get('/api/bootstrap').json())}
    assert set(due) == {oil, brakes}
    assert due[oil]['next_due_km'] == 123039
    assert due[brakes]['next_due_km'] == 128539


def test_case_and_whitespace_variants_share_one_service_plan(mileage_client):
    client = mileage_client
    vehicle = create_vehicle(client)
    create_service(client, vehicle['id'])
    latest = create_service(client, vehicle['id'], service_name='  CAMBIO   DE ACEITE  ',
                            performed_on='2026-10-13', odometer=120000)
    snapshot = client.get('/api/bootstrap').json()
    assert [plan['id'] for plan in snapshot['maintenance_plans']] == [latest]
    assert [plan['id'] for plan in frontend_due(snapshot)] == [latest]


def test_effective_date_ties_use_odometer_then_id(mileage_client):
    client = mileage_client
    vehicle = create_vehicle(client)
    create_service(client, vehicle['id'], odometer=120000)
    create_service(client, vehicle['id'], odometer=119000)
    snapshot = client.get('/api/bootstrap').json()
    assert frontend_due(snapshot)[0]['odometer'] == 120000
    tied_latest = create_service(client, vehicle['id'], odometer=120000, interval_km=6000)
    plan = frontend_due(client.get('/api/bootstrap').json())[0]
    assert plan['id'] == tied_latest
    assert plan['next_due_km'] == 126000


def correction_payload(vehicle, **changes):
    return {
        'odometer': 118000, 'expected_odometer': vehicle['odometer'],
        'reason': 'Lectura digitada por error, verificada en el odometro',
        'responsible': 'Responsable de flota', 'corrected_on': '2026-10-13',
        **changes,
    }


def test_explicit_correction_preserves_history_and_records_justification(mileage_client):
    client = mileage_client
    vehicle = create_vehicle(client)
    service_id = create_service(client, vehicle['id'])
    before_history = client.get('/api/bootstrap').json()['maintenance']
    response = client.post(f"/api/vehicles/{vehicle['id']}/odometer-correction",
                           json=correction_payload(vehicle))
    assert response.status_code == 200, response.text
    assert read_vehicle(client, vehicle['id'])['odometer'] == 118000
    snapshot = client.get('/api/bootstrap').json()
    old = next(row for row in snapshot['maintenance'] if row['id'] == service_id)
    assert old == before_history[0]
    audit = next(row for row in snapshot['maintenance'] if row['id'] != service_id)
    assert audit['interval_km'] is None
    assert audit['next_due_km'] is None
    assert audit['odometer'] == 118000
    assert '118539' in audit['notes']
    assert '118000' in audit['notes']
    assert 'Responsable de flota' in audit['notes']
    assert 'Lectura digitada por error' in audit['notes']
    assert [plan['id'] for plan in snapshot['maintenance_plans']] == [service_id]


@pytest.mark.parametrize('changes', [
    {'reason': ''}, {'reason': '   '}, {'responsible': ''},
    {'corrected_on': '2026-02-30'}, {'odometer': -1}, {'odometer': 2**63},
])
def test_invalid_explicit_correction_rejected_without_changes(mileage_client, changes):
    client = mileage_client
    vehicle = create_vehicle(client)
    response = client.post(f"/api/vehicles/{vehicle['id']}/odometer-correction",
                           json=correction_payload(vehicle, **changes))
    assert response.status_code == 422, response.text
    assert read_vehicle(client, vehicle['id']) == vehicle
    assert client.get('/api/bootstrap').json()['maintenance'] == []


def test_stale_explicit_correction_is_rejected_without_audit_write(mileage_client):
    client = mileage_client
    vehicle = create_vehicle(client)
    create_service(client, vehicle['id'], odometer=120000)
    before = client.get('/api/bootstrap').json()
    response = client.post(f"/api/vehicles/{vehicle['id']}/odometer-correction",
                           json=correction_payload(vehicle))
    assert response.status_code == 409, response.text
    after = client.get('/api/bootstrap').json()
    assert after['vehicles'] == before['vehicles']
    assert after['maintenance'] == before['maintenance']


def test_correction_requires_expected_reading_and_justification(mileage_client):
    client = mileage_client
    vehicle = create_vehicle(client)
    for missing in ('reason', 'responsible', 'expected_odometer', 'corrected_on'):
        payload = correction_payload(vehicle)
        payload.pop(missing)
        response = client.post(f"/api/vehicles/{vehicle['id']}/odometer-correction", json=payload)
        assert response.status_code == 422, (missing, response.text)
    assert read_vehicle(client, vehicle['id']) == vehicle
    assert client.get('/api/bootstrap').json()['maintenance'] == []


@pytest.mark.parametrize('changes', [
    {'reason': 'x' * 501}, {'responsible': 'x' * 101}, {'odometer': 118539},
])
def test_correction_does_not_truncate_or_create_empty_changes(mileage_client, changes):
    client = mileage_client
    vehicle = create_vehicle(client)
    response = client.post(f"/api/vehicles/{vehicle['id']}/odometer-correction",
                           json=correction_payload(vehicle, **changes))
    assert response.status_code == 422, response.text
    assert read_vehicle(client, vehicle['id']) == vehicle
    assert client.get('/api/bootstrap').json()['maintenance'] == []


def test_legacy_out_of_range_odometer_can_be_corrected_explicitly(mileage_client):
    import server

    client = mileage_client
    vehicle = create_vehicle(client)
    with server.connect() as connection:
        connection.execute('UPDATE vehicles SET odometer=20000000 WHERE id=?', (vehicle['id'],))
    legacy = read_vehicle(client, vehicle['id'])
    response = client.post(f"/api/vehicles/{vehicle['id']}/odometer-correction",
                           json=correction_payload(legacy, odometer=100000))
    assert response.status_code == 200, response.text
    assert read_vehicle(client, vehicle['id'])['odometer'] == 100000
    audit = client.get('/api/bootstrap').json()['maintenance'][0]
    assert '20000000' in audit['notes']
    assert '100000' in audit['notes']


def test_existing_invalid_date_is_retained_but_does_not_override_valid_plan(mileage_client):
    import server

    client = mileage_client
    vehicle = create_vehicle(client)
    current_id = create_service(client, vehicle['id'])
    with server.connect() as connection:
        connection.execute('''INSERT INTO maintenance
            (vehicle_id,service_name,performed_on,odometer,interval_km,next_due_km,notes)
            VALUES(?,?,?,?,?,?,?)''',
            (vehicle['id'], 'Cambio de aceite', 'fecha inexistente', 120000, 4500, 124500, 'Legacy'))
    snapshot = client.get('/api/bootstrap').json()
    assert len(snapshot['maintenance']) == 2
    assert any(row['performed_on'] == 'fecha inexistente' for row in snapshot['maintenance'])
    assert [plan['id'] for plan in snapshot['maintenance_plans']] == [current_id]
    assert [plan['id'] for plan in frontend_due(snapshot)] == [current_id]


@pytest.mark.parametrize('stored_target', [1, None])
def test_legacy_target_is_recalculated_without_rewriting_history(mileage_client, stored_target):
    import server

    client = mileage_client
    vehicle = create_vehicle(client)
    service_id = create_service(client, vehicle['id'], interval_km=8000)
    with server.connect() as connection:
        connection.execute('UPDATE maintenance SET next_due_km=? WHERE id=?', (stored_target, service_id))
    snapshot = client.get('/api/bootstrap').json()
    assert snapshot['maintenance'][0]['next_due_km'] == stored_target
    assert snapshot['maintenance_plans'][0]['next_due_km'] == 126539
    assert frontend_due(snapshot)[0]['next_due_km'] == 126539
    with server.connect() as connection:
        assert connection.execute('SELECT next_due_km FROM maintenance WHERE id=?',
                                  (service_id,)).fetchone()[0] == stored_target


@pytest.mark.parametrize('remaining, badge', [(501, 'ok'), (500, 'warn'), (1, 'warn'), (0, 'bad'), (-1, 'bad')])
def test_remaining_kilometers_and_warning_threshold(mileage_client, remaining, badge):
    client = mileage_client
    vehicle = create_vehicle(client, odometer=123039 - remaining)
    create_service(client, vehicle['id'])
    snapshot = client.get('/api/bootstrap').json()
    due = frontend_due(snapshot)
    assert due[0]['next_due_km'] == 123039
    assert due[0]['kmLeft'] == remaining
    rendered = frontend_eval(snapshot, 'renderMaintenance()')
    assert f'class="badge {badge}"' in rendered
    if remaining <= 0:
        assert 'Vencido' in rendered


def test_maintenance_transaction_rolls_back_when_vehicle_update_fails(mileage_client):
    import server

    client = mileage_client
    vehicle = create_vehicle(client)
    with server.connect() as connection:
        connection.execute('''CREATE TRIGGER reject_odometer_update BEFORE UPDATE ON vehicles
            BEGIN SELECT RAISE(ABORT, 'Injected update failure'); END''')
    with pytest.raises(sqlite3.IntegrityError, match='Injected update failure'):
        create_service(client, vehicle['id'], odometer=120000)
    assert read_vehicle(client, vehicle['id']) == vehicle
    assert client.get('/api/bootstrap').json()['maintenance'] == []


def test_explicit_correction_transaction_rolls_back_when_vehicle_update_fails(mileage_client):
    import server

    client = mileage_client
    vehicle = create_vehicle(client)
    with server.connect() as connection:
        connection.execute('''CREATE TRIGGER reject_odometer_update BEFORE UPDATE ON vehicles
            BEGIN SELECT RAISE(ABORT, 'Injected correction failure'); END''')
    with pytest.raises(sqlite3.IntegrityError, match='Injected correction failure'):
        client.post(f"/api/vehicles/{vehicle['id']}/odometer-correction",
                    json=correction_payload(vehicle))
    assert read_vehicle(client, vehicle['id']) == vehicle
    assert client.get('/api/bootstrap').json()['maintenance'] == []


def test_no_schema_migration_needed_for_odometer_and_plan_changes(mileage_client):
    import server

    client = mileage_client
    with server.connect() as connection:
        before = connection.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall()
        before = [tuple(row) for row in before]
        version = connection.execute('PRAGMA user_version').fetchone()[0]
    vehicle = create_vehicle(client)
    create_service(client, vehicle['id'])
    response = client.post(f"/api/vehicles/{vehicle['id']}/odometer-correction",
                           json=correction_payload(vehicle))
    assert response.status_code == 200, response.text
    with server.connect() as connection:
        after = [tuple(row) for row in connection.execute('SELECT name,sql FROM sqlite_master ORDER BY name')]
        assert after == before
        assert connection.execute('PRAGMA user_version').fetchone()[0] == version
