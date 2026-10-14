"""Inventory retry regressions. All writes target pytest's temporary databases."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def inventory_client(monkeypatch, tmp_path):
    import server

    monkeypatch.setenv('GEFLEET_DB', str(tmp_path / 'inventory.db'))
    monkeypatch.setattr(server, 'DB', tmp_path / 'inventory.db')
    with TestClient(server.app) as client:
        yield client


def create_vehicle(client):
    response = client.post('/api/vehicles', json={
        'plate': 'INV-001', 'name': 'Temporary test vehicle',
        'vehicle_type': 'LIVIANO', 'odometer': 1000,
    })
    assert response.status_code == 200, response.text
    return client.get('/api/bootstrap').json()['vehicles'][0]['id']


def create_part(client, code='INV-P01'):
    response = client.post('/api/parts', json={
        'code': code, 'name': f'Temporary part {code}', 'unit': 'unidad',
    })
    assert response.status_code == 200, response.text
    return response.json()['id']


def movement_payload(part_id, kind='ENTRADA', quantity='5'):
    return {
        'part_id': part_id, 'movement_type': kind, 'quantity': quantity,
        'responsible': 'Bodega', 'reference': 'INV-REGRESSION',
    }


def send_movement(client, payload, key=None):
    return client.post('/api/movements', json=payload,
                       headers={'Idempotency-Key': key or str(uuid4())})


def approved_request(client, vehicle_id, part_id, quantity='4'):
    response = client.post('/api/requests', json={
        'vehicle_id': vehicle_id, 'requester': 'Test requester',
        'items': [{'part_id': part_id, 'quantity': quantity}],
    })
    assert response.status_code == 200, response.text
    request_id = response.json()['id']
    response = client.post(f'/api/requests/{request_id}/approve',
                           json={'approved_by': 'Test approver'})
    assert response.status_code == 200, response.text
    line_id = client.get(f'/api/requests/{request_id}').json()['lines'][0]['id']
    return request_id, line_id


def delivery_payload(line_id, quantity='1'):
    return {
        'received_by': 'Test receiver', 'delivered_by': 'Test warehouse',
        'lines': [{'line_id': line_id, 'quantity': quantity}],
    }


def send_delivery(client, request_id, payload, key=None):
    return client.post(f'/api/requests/{request_id}/deliver', json=payload,
                       headers={'Idempotency-Key': key or str(uuid4())})


@pytest.mark.parametrize('kind,expected_stock', [('ENTRADA', 5000), ('SALIDA', 7000)])
def test_h04_movement_replay_does_not_change_stock_twice(inventory_client, kind, expected_stock):
    client = inventory_client
    part_id = create_part(client)
    existing_movements = 0
    if kind == 'SALIDA':
        assert send_movement(client, movement_payload(part_id, quantity='10')).status_code == 200
        existing_movements = 1
    payload = movement_payload(part_id, kind, '5' if kind == 'ENTRADA' else '3')
    key = str(uuid4())
    first = send_movement(client, payload, key)
    replay = send_movement(client, payload, key)
    assert first.status_code == replay.status_code == 200
    data = client.get('/api/bootstrap').json()
    assert len(data['movements']) == existing_movements + 1
    assert data['parts'][0]['stock_milli'] == expected_stock
    assert replay.json() == first.json()


def test_h04_partial_delivery_replay_does_not_deliver_twice(inventory_client):
    client = inventory_client
    vehicle_id, part_id = create_vehicle(client), create_part(client)
    assert send_movement(client, movement_payload(part_id)).status_code == 200
    request_id, line_id = approved_request(client, vehicle_id, part_id)
    payload, key = delivery_payload(line_id), str(uuid4())
    first = send_delivery(client, request_id, payload, key)
    replay = send_delivery(client, request_id, payload, key)
    assert first.status_code == replay.status_code == 200
    detail = client.get(f'/api/requests/{request_id}').json()
    assert detail['lines'][0]['delivered_milli'] == 1000
    assert detail['status'] == 'PARCIAL'
    data = client.get('/api/bootstrap').json()
    assert len(data['movements']) == 2
    assert data['parts'][0]['stock_milli'] == 4000
    assert replay.json() == first.json()


def operation_count(key=None):
    import server

    with sqlite3.connect(server.DB) as connection:
        if key is None:
            return connection.execute('SELECT COUNT(*) FROM inventory_operations').fetchone()[0]
        return connection.execute(
            'SELECT COUNT(*) FROM inventory_operations WHERE operation_key=?', (key,),
        ).fetchone()[0]


@pytest.mark.parametrize('field,value', [
    ('quantity', '2'), ('movement_type', 'SALIDA'),
    ('responsible', 'Different warehouse'), ('reference', 'OTHER'),
    ('notes', 'Different notes'),
])
def test_reused_movement_key_with_changed_content_is_conflict(inventory_client, field, value):
    client = inventory_client
    part_id = create_part(client)
    payload, key = movement_payload(part_id), str(uuid4())
    first = send_movement(client, payload, key)
    assert first.status_code == 200, first.text
    response = send_movement(client, {**payload, field: value}, key)
    assert response.status_code == 409, response.text
    assert operation_count(key) == 1
    data = client.get('/api/bootstrap').json()
    assert len(data['movements']) == 1
    assert data['parts'][0]['stock_milli'] == 5000
    assert send_movement(client, payload, key).json() == first.json()


def test_reused_delivery_key_with_changed_content_is_conflict(inventory_client):
    client = inventory_client
    vehicle_id, part_id = create_vehicle(client), create_part(client)
    assert send_movement(client, movement_payload(part_id)).status_code == 200
    request_id, line_id = approved_request(client, vehicle_id, part_id)
    key = str(uuid4())
    assert send_delivery(client, request_id, delivery_payload(line_id), key).status_code == 200
    response = send_delivery(client, request_id, delivery_payload(line_id, '2'), key)
    assert response.status_code == 409, response.text
    assert client.get(f'/api/requests/{request_id}').json()['lines'][0]['delivered_milli'] == 1000
    assert operation_count(key) == 1


def test_key_cannot_be_reused_for_different_endpoint(inventory_client):
    client = inventory_client
    vehicle_id, part_id = create_vehicle(client), create_part(client)
    key = str(uuid4())
    assert send_movement(client, movement_payload(part_id), key).status_code == 200
    request_id, line_id = approved_request(client, vehicle_id, part_id)
    response = send_delivery(client, request_id, delivery_payload(line_id), key)
    assert response.status_code == 409, response.text
    assert client.get(f'/api/requests/{request_id}').json()['lines'][0]['delivered_milli'] == 0
    assert len(client.get('/api/bootstrap').json()['movements']) == 1


def test_delivery_key_cannot_be_reused_for_other_request(inventory_client):
    client = inventory_client
    vehicle_id, part_id = create_vehicle(client), create_part(client)
    assert send_movement(client, movement_payload(part_id)).status_code == 200
    first_request, first_line = approved_request(client, vehicle_id, part_id)
    second_request, second_line = approved_request(client, vehicle_id, part_id)
    key = str(uuid4())
    assert send_delivery(client, first_request, delivery_payload(first_line), key).status_code == 200
    response = send_delivery(client, second_request, delivery_payload(second_line), key)
    assert response.status_code == 409, response.text
    assert client.get(f'/api/requests/{second_request}').json()['lines'][0]['delivered_milli'] == 0
    assert len(client.get('/api/bootstrap').json()['movements']) == 2


@pytest.mark.parametrize('operation', ['movement', 'delivery'])
def test_key_is_required_without_side_effects(inventory_client, operation):
    client = inventory_client
    vehicle_id, part_id = create_vehicle(client), create_part(client)
    if operation == 'movement':
        url, payload = '/api/movements', movement_payload(part_id)
    else:
        assert send_movement(client, movement_payload(part_id)).status_code == 200
        request_id, line_id = approved_request(client, vehicle_id, part_id)
        url, payload = f'/api/requests/{request_id}/deliver', delivery_payload(line_id)
    before = client.get('/api/bootstrap').json()
    before_operations = operation_count()
    response = client.post(url, json=payload)
    assert response.status_code == 422, response.text
    assert client.get('/api/bootstrap').json() == before
    assert operation_count() == before_operations


@pytest.mark.parametrize('key', ['', '   ', 'with spaces', 'bad/key', 'x' * 129])
def test_invalid_key_is_rejected_without_movement(inventory_client, key):
    client = inventory_client
    part_id = create_part(client)
    response = client.post('/api/movements', json=movement_payload(part_id),
                           headers={'Idempotency-Key': key})
    assert response.status_code == 422, response.text
    assert client.get('/api/bootstrap').json()['movements'] == []
    assert operation_count() == 0


def test_payload_order_and_explicit_defaults_keep_same_operation(inventory_client):
    client = inventory_client
    part_id = create_part(client)
    payload, key = movement_payload(part_id), str(uuid4())
    first = send_movement(client, payload, key)
    assert first.status_code == 200, first.text
    reordered = dict(reversed(list(payload.items())))
    reordered.update({'vehicle_id': None, 'notes': ''})
    replay = send_movement(client, reordered, key)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert len(client.get('/api/bootstrap').json()['movements']) == 1


@pytest.mark.parametrize('operation', ['movement', 'delivery'])
def test_concurrent_same_key_has_single_stock_effect(inventory_client, operation):
    client = inventory_client
    vehicle_id, part_id = create_vehicle(client), create_part(client)
    key, barrier = str(uuid4()), Barrier(2)
    if operation == 'delivery':
        assert send_movement(client, movement_payload(part_id)).status_code == 200
        request_id, line_id = approved_request(client, vehicle_id, part_id)
        payload = delivery_payload(line_id)
        expected_movements, expected_stock = 2, 4000
    else:
        payload = movement_payload(part_id)
        expected_movements, expected_stock = 1, 5000

    def send():
        barrier.wait(timeout=5)
        if operation == 'delivery':
            return send_delivery(client, request_id, payload, key)
        return send_movement(client, payload, key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(send) for _ in range(2)]
        responses = [future.result(timeout=20) for future in futures]
    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    assert operation_count(key) == 1
    data = client.get('/api/bootstrap').json()
    assert len(data['movements']) == expected_movements
    assert data['parts'][0]['stock_milli'] == expected_stock
    if operation == 'delivery':
        assert client.get(f'/api/requests/{request_id}').json()['lines'][0]['delivered_milli'] == 1000


def test_concurrent_different_content_under_same_key_has_one_conflict(inventory_client):
    client = inventory_client
    part_id, key, barrier = create_part(client), str(uuid4()), Barrier(2)

    def send(quantity):
        barrier.wait(timeout=5)
        return send_movement(client, movement_payload(part_id, quantity=quantity), key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(send, quantity) for quantity in ('2', '3')]
        responses = [future.result(timeout=20) for future in futures]
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert operation_count(key) == 1
    data = client.get('/api/bootstrap').json()
    assert len(data['movements']) == 1
    assert data['parts'][0]['stock_milli'] in (2000, 3000)


def test_distinct_keys_allow_legitimate_identical_deliveries(inventory_client):
    client = inventory_client
    vehicle_id, part_id = create_vehicle(client), create_part(client)
    assert send_movement(client, movement_payload(part_id)).status_code == 200
    request_id, line_id = approved_request(client, vehicle_id, part_id, '2')
    payload, first_key, second_key = delivery_payload(line_id), str(uuid4()), str(uuid4())
    first = send_delivery(client, request_id, payload, first_key)
    second = send_delivery(client, request_id, payload, second_key)
    assert first.status_code == second.status_code == 200
    assert first.json()['status'] == 'PARCIAL'
    assert second.json()['status'] == 'ENTREGADA'
    detail = client.get(f'/api/requests/{request_id}').json()
    assert detail['lines'][0]['delivered_milli'] == 2000
    assert detail['status'] == 'ENTREGADA'
    data = client.get('/api/bootstrap').json()
    assert len(data['movements']) == 3
    assert data['parts'][0]['stock_milli'] == 3000
    # Replays return the original result even after the request is completed.
    assert send_delivery(client, request_id, payload, first_key).json() == first.json()
    assert send_delivery(client, request_id, payload, second_key).json() == second.json()
    assert len(client.get('/api/bootstrap').json()['movements']) == 3
    assert operation_count(first_key) == operation_count(second_key) == 1


@pytest.mark.parametrize('operation', ['movement', 'delivery'])
def test_insufficient_stock_rolls_back_and_key_can_retry_after_restock(inventory_client, operation):
    client = inventory_client
    vehicle_id, part_id, key = create_vehicle(client), create_part(client), str(uuid4())
    if operation == 'movement':
        payload = movement_payload(part_id, 'SALIDA', '1')
        send = lambda: send_movement(client, payload, key)
    else:
        request_id, line_id = approved_request(client, vehicle_id, part_id)
        payload = delivery_payload(line_id)
        send = lambda: send_delivery(client, request_id, payload, key)
    before = client.get('/api/bootstrap').json()
    response = send()
    assert response.status_code == 409, response.text
    assert client.get('/api/bootstrap').json() == before
    assert operation_count(key) == 0
    assert send_movement(client, movement_payload(part_id, quantity='1')).status_code == 200
    response = send()
    assert response.status_code == 200, response.text
    assert operation_count(key) == 1
    assert client.get('/api/bootstrap').json()['parts'][0]['stock_milli'] == 0


@pytest.mark.parametrize('operation', ['movement', 'delivery'])
def test_failure_recording_operation_rolls_back_all_business_changes(inventory_client, operation):
    import server

    client = inventory_client
    vehicle_id, part_id, key = create_vehicle(client), create_part(client), str(uuid4())
    if operation == 'movement':
        payload = movement_payload(part_id)
        send = lambda: send_movement(client, payload, key)
    else:
        assert send_movement(client, movement_payload(part_id)).status_code == 200
        request_id, line_id = approved_request(client, vehicle_id, part_id)
        payload = delivery_payload(line_id)
        send = lambda: send_delivery(client, request_id, payload, key)
    before = client.get('/api/bootstrap').json()
    before_operations = operation_count()
    with sqlite3.connect(server.DB) as connection:
        connection.execute('''CREATE TRIGGER fail_inventory_operation
            BEFORE INSERT ON inventory_operations BEGIN
            SELECT RAISE(ABORT, 'forced operation persistence failure'); END''')
    try:
        with pytest.raises(sqlite3.DatabaseError, match='forced operation persistence failure'):
            send()
        assert client.get('/api/bootstrap').json() == before
        assert operation_count() == before_operations
        assert operation_count(key) == 0
    finally:
        with sqlite3.connect(server.DB) as connection:
            connection.execute('DROP TRIGGER fail_inventory_operation')
    response = send()
    assert response.status_code == 200, response.text
    assert operation_count(key) == 1


def test_multiline_delivery_error_rolls_back_earlier_line_and_operation(inventory_client):
    client = inventory_client
    vehicle_id, first_part = create_vehicle(client), create_part(client)
    second_part = create_part(client, 'INV-P02')
    assert send_movement(client, movement_payload(first_part)).status_code == 200
    response = client.post('/api/requests', json={
        'vehicle_id': vehicle_id, 'requester': 'Test requester',
        'items': [{'part_id': first_part, 'quantity': '1'},
                  {'part_id': second_part, 'quantity': '1'}],
    })
    assert response.status_code == 200, response.text
    request_id = response.json()['id']
    assert client.post(f'/api/requests/{request_id}/approve',
                       json={'approved_by': 'Test approver'}).status_code == 200
    lines = client.get(f'/api/requests/{request_id}').json()['lines']
    payload = {
        'received_by': 'Test receiver', 'delivered_by': 'Test warehouse',
        'lines': [{'line_id': line['id'], 'quantity': '1'} for line in lines],
    }
    key = str(uuid4())
    before = client.get('/api/bootstrap').json()
    response = send_delivery(client, request_id, payload, key)
    assert response.status_code == 409, response.text
    assert client.get('/api/bootstrap').json() == before
    assert operation_count(key) == 0
    assert send_movement(client, movement_payload(second_part, quantity='1')).status_code == 200
    response = send_delivery(client, request_id, payload, key)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'ENTREGADA'
    assert all(line['delivered_milli'] == 1000
               for line in client.get(f'/api/requests/{request_id}').json()['lines'])


def test_request_must_be_approved_and_can_retry_same_key_after_approval(inventory_client):
    client = inventory_client
    vehicle_id, part_id = create_vehicle(client), create_part(client)
    assert send_movement(client, movement_payload(part_id)).status_code == 200
    response = client.post('/api/requests', json={
        'vehicle_id': vehicle_id, 'requester': 'Test requester',
        'items': [{'part_id': part_id, 'quantity': '1'}],
    })
    request_id = response.json()['id']
    line_id = client.get(f'/api/requests/{request_id}').json()['lines'][0]['id']
    payload, key = delivery_payload(line_id), str(uuid4())
    response = send_delivery(client, request_id, payload, key)
    assert response.status_code == 409, response.text
    assert operation_count(key) == 0
    assert client.get('/api/bootstrap').json()['parts'][0]['stock_milli'] == 5000
    assert client.post(f'/api/requests/{request_id}/approve',
                       json={'approved_by': 'Test approver'}).status_code == 200
    response = send_delivery(client, request_id, payload, key)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'ENTREGADA'


def test_replays_preserve_kardex_rows_and_csv(inventory_client):
    client = inventory_client
    vehicle_id, part_id = create_vehicle(client), create_part(client)
    entry_payload, entry_key = movement_payload(part_id), str(uuid4())
    exit_payload, exit_key = movement_payload(part_id, 'SALIDA', '1'), str(uuid4())
    exit_payload['vehicle_id'] = vehicle_id
    assert send_movement(client, entry_payload, entry_key).status_code == 200
    assert send_movement(client, exit_payload, exit_key).status_code == 200
    before_rows = client.get('/api/bootstrap').json()['movements']
    before_csv = client.get('/api/export/movements').content
    for _ in range(3):
        assert send_movement(client, entry_payload, entry_key).status_code == 200
        assert send_movement(client, exit_payload, exit_key).status_code == 200
    assert client.get('/api/bootstrap').json()['movements'] == before_rows
    assert client.get('/api/export/movements').content == before_csv
    assert client.get('/api/bootstrap').json()['parts'][0]['stock_milli'] == 4000
    assert operation_count() == 2


def test_unique_database_key_prevents_duplicate_operation_records(inventory_client):
    import server

    client = inventory_client
    part_id, key = create_part(client), str(uuid4())
    assert send_movement(client, movement_payload(part_id), key).status_code == 200
    with sqlite3.connect(server.DB) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute('''INSERT INTO inventory_operations
                (operation_key,scope,request_hash,response_json)
                SELECT operation_key,scope,request_hash,response_json
                FROM inventory_operations WHERE operation_key=?''', (key,))
    assert operation_count(key) == 1


def test_migration_from_version_zero_preserves_history_and_is_repeatable(inventory_client):
    import server

    client = inventory_client
    vehicle_id, part_id = create_vehicle(client), create_part(client)
    assert send_movement(client, movement_payload(part_id)).status_code == 200
    request_id, line_id = approved_request(client, vehicle_id, part_id)
    assert send_delivery(client, request_id, delivery_payload(line_id)).status_code == 200
    business_tables = ['vehicles', 'parts', 'compatibility', 'movements',
                       'requests', 'request_lines', 'inspections', 'maintenance']
    with sqlite3.connect(server.DB) as connection:
        # The previous schema has the same business tables, no operation table
        # and user_version=0. No historical row is altered to build this fixture.
        connection.execute('DROP TABLE inventory_operations')
        connection.execute('PRAGMA user_version=0')
        before = {table: connection.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
                  for table in business_tables}
    before_stock = client.get('/api/bootstrap').json()['parts'][0]['stock_milli']
    before_csv = client.get('/api/export/movements').content
    for _ in range(2):
        server.init_db()
        with sqlite3.connect(server.DB) as connection:
            after = {table: connection.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
                     for table in business_tables}
            assert after == before
            assert connection.execute('PRAGMA user_version').fetchone()[0] == 1
            assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
            assert connection.execute('SELECT COUNT(*) FROM inventory_operations').fetchone()[0] == 0
        assert client.get('/api/bootstrap').json()['parts'][0]['stock_milli'] == before_stock
        assert client.get('/api/export/movements').content == before_csv
    assert send_movement(client, movement_payload(part_id, quantity='1')).status_code == 200
    assert client.get('/api/bootstrap').json()['parts'][0]['stock_milli'] == before_stock + 1000
