"""Checklist API regressions. Every request uses an isolated temporary SQLite DB."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def checklist_client(monkeypatch, tmp_path):
    database = tmp_path / 'checklist.db'
    monkeypatch.setenv('GEFLEET_ALLOW_CREATE_DB', '1')
    monkeypatch.setenv('GEFLEET_DB', str(database))
    import server
    monkeypatch.setattr(server, 'DB', database)
    with TestClient(server.app) as client:
        yield client


@pytest.fixture()
def complete_checklist(checklist_client):
    response = checklist_client.post('/api/vehicles', json={
        'plate': 'CHECK-01', 'name': 'Vehículo de checklist',
        'vehicle_type': 'LIVIANO', 'odometer': 1000,
    })
    assert response.status_code == 200, response.text
    state = checklist_client.get('/api/bootstrap').json()
    answers = {
        f'{group_index}-{item_index}': 'BIEN'
        for group_index, group in enumerate(state['template'])
        for item_index, _ in enumerate(group['items'])
    }
    assert len(answers) == 37
    return {
        'client_uuid': 'checklist-regression-01',
        'vehicle_id': state['vehicles'][0]['id'],
        'date': '2026-10-12', 'inspector': 'Conductor de prueba',
        'odometer': 1500, 'answers': answers,
    }


def assert_not_finalized(client):
    state = client.get('/api/bootstrap').json()
    assert state['inspections'] == []
    assert state['vehicles'][0]['odometer'] == 1000


def test_observations_at_limit_preserve_every_character(checklist_client, complete_checklist):
    observations = '  \n' + 'á' * 2493 + '\n  \n'
    assert len(observations) == 2500
    response = checklist_client.post('/api/inspections', json={
        **complete_checklist, 'observations': observations,
    })
    assert response.status_code == 200, response.text
    detail = checklist_client.get(f"/api/inspections/{response.json()['id']}").json()
    assert detail['observations'] == observations
    assert checklist_client.get('/api/bootstrap').json()['inspections'][0]['observations'] == observations


def test_observations_over_limit_reject_without_side_effects(checklist_client, complete_checklist):
    response = checklist_client.post('/api/inspections', json={
        **complete_checklist, 'observations': 'á' * 2501,
    })
    assert response.status_code == 422, response.text
    assert '2500 caracteres' in response.json()['detail']
    assert 'no se ha guardado la inspección' in response.json()['detail']
    assert_not_finalized(checklist_client)
    # The rejected UUID is still usable once the text has been corrected.
    corrected = checklist_client.post('/api/inspections', json={
        **complete_checklist, 'observations': 'Observación corregida completa.',
    })
    assert corrected.status_code == 200, corrected.text
    assert not corrected.json().get('duplicate', False)
    assert len(checklist_client.get('/api/bootstrap').json()['inspections']) == 1


def test_mixed_answers_are_preserved_and_failures_counted(checklist_client, complete_checklist):
    choices = ('BIEN', 'MAL', 'NA')
    answers = {key: choices[index % 3] for index, key in enumerate(complete_checklist['answers'])}
    response = checklist_client.post('/api/inspections', json={
        **complete_checklist, 'answers': answers,
        'observations': 'Línea inicial\nObservación de falla\nLínea final',
    })
    assert response.status_code == 200, response.text
    detail = checklist_client.get(f"/api/inspections/{response.json()['id']}").json()
    assert detail['answers'] == answers
    assert detail['failure_count'] == sum(answer == 'MAL' for answer in answers.values())
    assert detail['inspector'] == complete_checklist['inspector']
    assert detail['vehicle_id'] == complete_checklist['vehicle_id']
    assert detail['date'] == complete_checklist['date']
    assert detail['odometer'] == complete_checklist['odometer']


@pytest.mark.parametrize('problem', ['missing', 'empty', 'invalid', 'extra'])
def test_incomplete_or_invalid_answers_never_finalize(checklist_client, complete_checklist, problem):
    answers = dict(complete_checklist['answers'])
    first = next(iter(answers))
    if problem == 'missing':
        del answers[first]
    elif problem == 'empty':
        answers[first] = ''
    elif problem == 'invalid':
        answers[first] = 'N/A'  # The API representation of N/A remains NA.
    else:
        answers['not-a-checklist-item'] = 'BIEN'
    response = checklist_client.post('/api/inspections', json={**complete_checklist, 'answers': answers})
    assert response.status_code == 422, response.text
    assert 'Responde cada ítem' in response.json()['detail']
    assert_not_finalized(checklist_client)


@pytest.mark.parametrize('field, value', [
    ('inspector', ''), ('inspector', '   '), ('date', ''),
    ('date', '2026-02-30'), ('odometer', -1), ('vehicle_id', 0),
])
def test_incomplete_header_never_finalizes(checklist_client, complete_checklist, field, value):
    response = checklist_client.post('/api/inspections', json={**complete_checklist, field: value})
    assert response.status_code in (422, 404), response.text
    assert_not_finalized(checklist_client)


@pytest.mark.parametrize('field', ['vehicle_id', 'date', 'inspector', 'odometer'])
def test_missing_header_fields_never_finalize(checklist_client, complete_checklist, field):
    payload = {key: value for key, value in complete_checklist.items() if key != field}
    response = checklist_client.post('/api/inspections', json=payload)
    assert response.status_code == 422, response.text
    assert_not_finalized(checklist_client)
