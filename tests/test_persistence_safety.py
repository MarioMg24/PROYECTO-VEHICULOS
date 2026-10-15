"""Migration/backup regressions. Every database and restoration is temporary."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

import server


BUSINESS_TABLES = tuple(server.BASE_TABLE_COLUMNS)


def build_database(path, version=None):
    """Real old-format relationships with non-empty history in every table."""
    if version is None:
        version = server.SCHEMA_VERSION
    with closing(sqlite3.connect(path, isolation_level=None)) as cn:
        cn.execute('PRAGMA foreign_keys=ON')
        cn.executescript(server.BASE_SCHEMA)
        if version >= 1:
            server._migration_1(cn)
        if version >= 2:
            server._migration_2(cn)
        cn.execute(f'PRAGMA user_version={version}')
        cn.execute("""INSERT INTO vehicles(id,plate,name,vehicle_type,brand,model,odometer,notes)
            VALUES(1,'LEG-001','Vehículo histórico','LIVIANO','Marca','Modelo',118539,'Conservar vehículo')""")
        cn.execute("""INSERT INTO inspections(id,client_uuid,vehicle_id,date,inspector,odometer,
                answers_json,observations,photos_json,failure_count)
            VALUES(1,'historical-checklist',1,'2026-10-01','Conductor',118000,?,?,?,1)""",
            (json.dumps({'0-0': 'BIEN', '0-1': 'MAL', '0-2': 'N/A'}),
             'Observación completa: revisar frenos.', '[]'))
        cn.execute("""INSERT INTO parts(id,code,name,unit,min_milli,notes)
            VALUES(1,'LEG-P01','Aceite histórico','galón',2000,'Conservar repuesto')""")
        cn.execute("INSERT INTO compatibility VALUES(1,1,'Compatible')")
        cn.execute("""INSERT INTO movements(id,part_id,movement_type,qty_milli,vehicle_id,reference,responsible,notes)
            VALUES(1,1,'ENTRADA',10000,NULL,'COMPRA-001','Bodega','Ingreso histórico')""")
        cn.execute("""INSERT INTO movements(id,part_id,movement_type,qty_milli,vehicle_id,reference,responsible,notes)
            VALUES(2,1,'SALIDA',1750,1,'SOL-00001','Bodega','Entrega parcial')""")
        cn.execute("""INSERT INTO maintenance(id,vehicle_id,service_name,performed_on,odometer,interval_km,next_due_km,notes)
            VALUES(1,1,'Cambio de aceite','2026-10-02',118539,4500,123039,'Conservar mantenimiento')""")
        cn.execute("""INSERT INTO requests(id,vehicle_id,requester,reason,status,approved_by,received_by)
            VALUES(1,1,'Mecánico','Servicio','PARCIAL','Jefe','Conductor')""")
        cn.execute('INSERT INTO request_lines VALUES(1,1,1,4000,1750)')
        if version >= 1:
            cn.execute("""INSERT INTO inventory_operations(operation_key,scope,request_hash,response_json)
                VALUES('historical-delivery-key','delivery:1','historical-hash',?)""",
                ('{"ok":true,"status":"PARCIAL","delivered_milli":1750}',))
    return path


def inspect(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as cn:
        names = sorted(r[0] for r in cn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
        return {
            'version': cn.execute('PRAGMA user_version').fetchone()[0],
            'schema': cn.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name').fetchall(),
            'data': {name: cn.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() for name in names},
        }


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_legacy_rows_preserved(after, before):
    for table in BUSINESS_TABLES:
        # Version 2 appends four optional maintenance fields. Every original
        # value must survive, and no historic row may be added or removed.
        original_rows = before['data'][table]
        updated_rows = after['data'][table]
        assert len(updated_rows) == len(original_rows)
        assert [row[:len(original)] for row, original in zip(updated_rows, original_rows)] == original_rows
        if table == 'maintenance':
            assert all(row[len(original):] == ('', '', '', '')
                       for row, original in zip(updated_rows, original_rows))


def assert_sound_snapshot(path, expected):
    with closing(sqlite3.connect(path)) as cn:
        assert cn.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert cn.execute('PRAGMA foreign_key_check').fetchall() == []
        assert cn.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        assert server.stock(cn, 1) == 8250
    assert inspect(path) == expected
    assert not Path(str(path) + '-wal').exists()
    assert not Path(str(path) + '-shm').exists()


@pytest.fixture
def persisted_client(monkeypatch, tmp_path):
    path = build_database(tmp_path / 'active.db')
    monkeypatch.setattr(server, 'DB', path)
    monkeypatch.setenv('GEFLEET_DB', str(path))
    monkeypatch.setenv('GEFLEET_ALLOW_CREATE_DB', '1')
    with TestClient(server.app) as client:
        yield client, path


def test_migrate_old_copy_preserves_every_business_record_and_original(monkeypatch, tmp_path):
    original = build_database(tmp_path / 'old.db', version=0)
    copy = tmp_path / 'migration-copy.db'
    shutil.copyfile(original, copy)
    original_hash = digest(original)
    before = inspect(copy)
    monkeypatch.setattr(server, 'DB', copy)
    server.init_db(allow_create=False)
    after = inspect(copy)
    assert after['version'] == server.SCHEMA_VERSION
    assert after['data']['inventory_operations'] == []
    assert_legacy_rows_preserved(after, before)
    assert digest(original) == original_hash
    assert inspect(original)['version'] == 0
    with closing(sqlite3.connect(copy)) as cn:
        assert server.validate_database(cn) == server.SCHEMA_VERSION
        assert server.stock(cn, 1) == 8250


def test_repeat_migration_does_not_duplicate_schema_or_records(monkeypatch, tmp_path):
    path = build_database(tmp_path / 'repeat.db', version=0)
    monkeypatch.setattr(server, 'DB', path)
    server.init_db(allow_create=False)
    once = inspect(path)
    server.init_db(allow_create=False)
    server.init_db(allow_create=False)
    assert inspect(path) == once


def test_future_migrations_run_in_order_exactly_once(monkeypatch, tmp_path):
    path = build_database(tmp_path / 'future.db')
    calls = []

    def third(cn):
        calls.append(3)
        cn.execute('CREATE TABLE migration_log(version INTEGER PRIMARY KEY)')
        cn.execute('INSERT INTO migration_log VALUES(3)')

    def fourth(cn):
        calls.append(4)
        cn.execute('INSERT INTO migration_log VALUES(4)')

    monkeypatch.setattr(server, 'SCHEMA_VERSION', 4)
    monkeypatch.setattr(server, 'MIGRATIONS', {1: server._migration_1, 2: server._migration_2, 3: third, 4: fourth})
    monkeypatch.setattr(server, 'DB', path)
    before = inspect(path)['data']
    server.init_db(allow_create=False)
    server.init_db(allow_create=False)
    after = inspect(path)
    assert calls == [3, 4]
    assert after['version'] == 4
    assert after['data']['migration_log'] == [(3,), (4,)]
    for table, rows in before.items():
        assert after['data'][table] == rows


def test_failed_future_migration_rolls_back_whole_chain_schema_data_and_version(monkeypatch, tmp_path):
    path = build_database(tmp_path / 'rollback.db')
    before = inspect(path)

    def third(cn):
        cn.execute('CREATE TABLE future_table(id INTEGER PRIMARY KEY, note TEXT)')
        cn.execute("INSERT INTO future_table VALUES(1,'No debe persistir')")
        cn.execute("UPDATE vehicles SET notes='No debe persistir'")

    def fourth(cn):
        cn.execute('ALTER TABLE vehicles ADD COLUMN future_column TEXT')
        raise RuntimeError('Simulated migration failure')

    monkeypatch.setattr(server, 'SCHEMA_VERSION', 4)
    monkeypatch.setattr(server, 'MIGRATIONS', {1: server._migration_1, 2: server._migration_2, 3: third, 4: fourth})
    monkeypatch.setattr(server, 'DB', path)
    with pytest.raises(RuntimeError, match='Simulated migration failure'):
        server.init_db(allow_create=False)
    assert inspect(path) == before


@pytest.mark.parametrize('attempt', ['COMMIT', 'ROLLBACK', 'executescript', 'PRAGMA user_version=99'])
def test_migration_callback_cannot_escape_transaction(monkeypatch, tmp_path, attempt):
    path = build_database(tmp_path / 'escape.db')
    before = inspect(path)

    def second(cn):
        cn.execute('CREATE TABLE forbidden_partial_update(id INTEGER)')
        cn.execute("UPDATE vehicles SET notes='No debe persistir'")
        if attempt == 'executescript':
            cn.executescript('CREATE TABLE escaping_script(id INTEGER);')
        else:
            cn.execute(attempt)

    monkeypatch.setattr(server, 'SCHEMA_VERSION', 3)
    monkeypatch.setattr(server, 'MIGRATIONS', {1: server._migration_1, 2: server._migration_2, 3: second})
    monkeypatch.setattr(server, 'DB', path)
    with pytest.raises(sqlite3.DatabaseError):
        server.init_db(allow_create=False)
    assert inspect(path) == before


def test_failed_initialization_rolls_back_all_base_tables(monkeypatch, tmp_path):
    path = tmp_path / 'new-failed.db'

    def fail(cn):
        server._migration_1(cn)
        raise RuntimeError('Failed initialization')

    monkeypatch.setattr(server, 'DB', path)
    monkeypatch.setattr(server, 'MIGRATIONS', {1: fail, 2: server._migration_2})
    with pytest.raises(RuntimeError, match='Failed initialization'):
        server.init_db(allow_create=True)
    state = inspect(path)
    assert state['version'] == 0
    assert state['schema'] == []
    assert state['data'] == {}


def test_missing_migration_fails_before_schema_or_data_changes(monkeypatch, tmp_path):
    path = build_database(tmp_path / 'gap.db')
    before = inspect(path)
    monkeypatch.setattr(server, 'DB', path)
    monkeypatch.setattr(server, 'SCHEMA_VERSION', 3)
    monkeypatch.setattr(server, 'MIGRATIONS', {1: server._migration_1, 3: lambda cn: None})
    with pytest.raises(RuntimeError, match='migraciones'):
        server.init_db(allow_create=False)
    assert inspect(path) == before


def test_newer_schema_is_rejected_without_changes(monkeypatch, tmp_path):
    path = build_database(tmp_path / 'newer.db', version=99)
    before = inspect(path)
    monkeypatch.setattr(server, 'DB', path)
    with pytest.raises(RuntimeError, match='compatible'):
        server.init_db(allow_create=False)
    assert inspect(path) == before


def test_incomplete_existing_schema_is_not_silently_reconstructed(monkeypatch, tmp_path):
    path = build_database(tmp_path / 'incomplete.db', version=0)
    with closing(sqlite3.connect(path)) as cn:
        cn.execute('DROP TABLE inspections')
        cn.commit()
    before = inspect(path)
    monkeypatch.setattr(server, 'DB', path)
    with pytest.raises(RuntimeError, match='inspections'):
        server.init_db(allow_create=True)
    assert inspect(path) == before


def test_version_zero_with_inventory_table_is_rejected_without_repair(monkeypatch, tmp_path):
    path = build_database(tmp_path / 'ambiguous-version.db', version=0)
    with closing(sqlite3.connect(path, isolation_level=None)) as cn:
        server._migration_1(cn)
    before = inspect(path)
    monkeypatch.setattr(server, 'DB', path)
    with pytest.raises(RuntimeError, match='versión 0'):
        server.init_db(allow_create=False)
    assert inspect(path) == before
    destination = tmp_path / 'must-not-restore.db'
    with pytest.raises(RuntimeError, match='versión 0'):
        server.restore_backup(path, destination)
    assert not destination.exists()
    assert inspect(path) == before


def test_concurrent_initialization_of_legacy_copy_migrates_once(monkeypatch, tmp_path):
    path = build_database(tmp_path / 'concurrent-old.db', version=0)
    with closing(sqlite3.connect(path)) as cn:
        cn.execute('PRAGMA journal_mode=WAL')
    monkeypatch.setattr(server, 'DB', path)
    barrier = Barrier(2)

    def migrate():
        barrier.wait(timeout=10)
        server.init_db(allow_create=False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(migrate) for _ in range(2)]
        for result in results:
            result.result(timeout=30)
    after = inspect(path)
    assert after['version'] == server.SCHEMA_VERSION
    assert len(after['data']['vehicles']) == 1
    assert len(after['data']['movements']) == 2
    assert after['data']['inventory_operations'] == []


@pytest.mark.parametrize('value', ['', '   ', 'relative/fleet.db'])
def test_invalid_database_paths_are_rejected(value):
    with pytest.raises(RuntimeError):
        server.resolve_database_path(value)


def test_missing_wrong_path_does_not_create_database_or_directory(monkeypatch, tmp_path):
    path = tmp_path / 'mistyped-folder' / 'fleet.db'
    monkeypatch.setattr(server, 'DB', path)
    monkeypatch.delenv('GEFLEET_ALLOW_CREATE_DB', raising=False)
    with pytest.raises(RuntimeError, match='db-init'):
        server.init_db()
    with pytest.raises(RuntimeError, match='db-init'):
        with server.connect():
            pytest.fail('Opening a missing database should fail')
    assert not path.exists()
    assert not path.parent.exists()


def test_explicit_creation_creates_only_requested_new_database(monkeypatch, tmp_path):
    path = tmp_path / 'intentional-new.db'
    monkeypatch.setattr(server, 'DB', path)
    monkeypatch.delenv('GEFLEET_ALLOW_CREATE_DB', raising=False)
    server.init_db(allow_create=True)
    state = inspect(path)
    assert state['version'] == server.SCHEMA_VERSION
    assert set(state['data']) == set(BUSINESS_TABLES) | {'inventory_operations'}
    assert all(not rows for rows in state['data'].values())


def test_creation_opt_in_still_does_not_create_missing_parent(monkeypatch, tmp_path):
    path = tmp_path / 'missing-parent' / 'new.db'
    monkeypatch.setattr(server, 'DB', path)
    monkeypatch.setenv('GEFLEET_ALLOW_CREATE_DB', '1')
    with pytest.raises(RuntimeError, match='carpeta'):
        server.init_db()
    assert not path.parent.exists()


def test_existing_empty_database_is_not_treated_as_new(monkeypatch, tmp_path):
    path = tmp_path / 'existing-empty.db'
    path.write_bytes(b'')
    monkeypatch.setattr(server, 'DB', path)
    with pytest.raises(RuntimeError, match='Esquema incompleto'):
        server.init_db(allow_create=True)
    assert inspect(path)['schema'] == []


def test_health_and_info_identify_actual_active_database(persisted_client):
    client, path = persisted_client
    before = inspect(path)
    health = client.get('/api/health').json()
    assert health['database'] == str(path.resolve())
    assert health['schema_version'] == server.SCHEMA_VERSION
    info = server.database_info()
    assert info['database'] == str(path.resolve())
    assert info['integrity_check'] == 'ok'
    assert info['foreign_key_check'] == []
    assert info['tables']['movements'] == 2
    assert info['tables']['inventory_operations'] == 1
    assert inspect(path) == before


@pytest.mark.parametrize('kind', ['helper', 'endpoint'])
def test_backup_captures_committed_wal_without_changing_source(persisted_client, tmp_path, kind):
    client, path = persisted_client
    with closing(sqlite3.connect(path, isolation_level=None)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute("UPDATE vehicles SET notes='Cambio confirmado solamente en WAL'")
        assert Path(str(path) + '-wal').stat().st_size > 0
        expected = inspect(path)
        destination = tmp_path / f'backup-{kind}.db'
        if kind == 'helper':
            server.create_backup(destination)
        else:
            response = client.get('/api/backup')
            assert response.status_code == 200, response.text
            assert response.headers['content-type'] == 'application/octet-stream'
            assert '.db' in response.headers['content-disposition']
            destination.write_bytes(response.content)
        assert_sound_snapshot(destination, expected)
        assert writer.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert inspect(path) == expected


def test_backup_excludes_uncommitted_changes(persisted_client, tmp_path):
    _, path = persisted_client
    expected = inspect(path)
    with closing(sqlite3.connect(path, isolation_level=None)) as writer:
        writer.execute('BEGIN IMMEDIATE')
        writer.execute("UPDATE vehicles SET notes='Uncommitted must not appear'")
        destination = tmp_path / 'committed-only.db'
        server.create_backup(destination)
        assert_sound_snapshot(destination, expected)
        writer.execute('ROLLBACK')


def test_backup_never_overwrites_existing_destination(persisted_client, tmp_path):
    destination = tmp_path / 'existing-backup.db'
    destination.write_bytes(b'previous backup must survive')
    before = digest(destination)
    with pytest.raises(RuntimeError, match='No se sobrescribe'):
        server.create_backup(destination)
    assert digest(destination) == before


def test_old_endpoint_wal_backup_can_be_normalized_and_restored(persisted_client, tmp_path):
    _, active = persisted_client
    expected = inspect(active)
    old_backup = tmp_path / 'old-endpoint-format.db'
    # Reproduce the previous /api/backup implementation exactly. Its complete
    # snapshot retains a WAL header even after closing all SQLite connections.
    with closing(sqlite3.connect(active)) as source:
        with closing(sqlite3.connect(':memory:')) as memory:
            source.backup(memory)
            with closing(sqlite3.connect(old_backup)) as output:
                memory.backup(output)
    assert old_backup.read_bytes()[18:20] == b'\x02\x02'
    assert not Path(str(old_backup) + '-wal').exists()
    assert not Path(str(old_backup) + '-shm').exists()
    restored = tmp_path / 'restored-old-endpoint.db'
    with pytest.raises(RuntimeError, match='formato WAL'):
        server.restore_backup(old_backup, restored)
    assert not restored.exists()
    original_hash = digest(old_backup)
    normalized = tmp_path / 'normalized-backup.db'
    server.create_backup(normalized, database=old_backup)
    assert_sound_snapshot(normalized, expected)
    assert digest(old_backup) == original_hash
    server.restore_backup(normalized, restored)
    assert_sound_snapshot(restored, expected)


def test_backup_of_invalid_source_does_not_publish_partial_file(tmp_path):
    source = build_database(tmp_path / 'invalid-source.db')
    with closing(sqlite3.connect(source)) as cn:
        cn.execute('PRAGMA foreign_keys=OFF')
        cn.execute('UPDATE movements SET part_id=999 WHERE id=2')
        cn.commit()
    source_hash = digest(source)
    destination = tmp_path / 'must-not-publish.db'
    with pytest.raises(RuntimeError, match='claves foráneas'):
        server.create_backup(destination, database=source)
    assert not destination.exists()
    assert not list(tmp_path.glob('.gefleet-snapshot-*'))
    assert digest(source) == source_hash


def test_restore_preserves_all_history_quantities_and_idempotency_keys(tmp_path):
    source = build_database(tmp_path / 'validated-backup.db')
    source_hash = digest(source)
    expected = inspect(source)
    destination = tmp_path / 'restored.db'
    info = server.restore_backup(source, destination)
    assert info['database'] == str(destination.resolve())
    assert_sound_snapshot(destination, expected)
    assert digest(source) == source_hash
    assert inspect(destination)['data']['inventory_operations'][0][0] == 'historical-delivery-key'


def test_restore_old_backup_preserves_version_then_migrates_copy(monkeypatch, tmp_path):
    source = build_database(tmp_path / 'old-backup.db', version=0)
    source_hash = digest(source)
    before = inspect(source)
    destination = tmp_path / 'restored-old.db'
    server.restore_backup(source, destination)
    assert_sound_snapshot(destination, before)
    monkeypatch.setattr(server, 'DB', destination)
    server.init_db(allow_create=False)
    after = inspect(destination)
    assert after['version'] == server.SCHEMA_VERSION
    assert_legacy_rows_preserved(after, before)
    assert digest(source) == source_hash


def test_restore_never_overwrites_newer_destination(tmp_path):
    source = build_database(tmp_path / 'older-backup.db')
    destination = build_database(tmp_path / 'newer-data.db')
    with closing(sqlite3.connect(destination)) as cn:
        cn.execute("UPDATE vehicles SET notes='Información más reciente que el respaldo'")
        cn.commit()
    before = digest(destination)
    with pytest.raises(RuntimeError, match='No se sobrescribe'):
        server.restore_backup(source, destination)
    assert digest(destination) == before


@pytest.mark.parametrize('problem', ['corrupt', 'corrupt-pages', 'foreign-key', 'future-schema', 'incomplete-schema'])
def test_invalid_backup_is_rejected_before_creating_destination(tmp_path, problem):
    source = tmp_path / 'bad-backup.db'
    if problem == 'corrupt':
        source.write_bytes(b'Not a SQLite backup')
    else:
        build_database(source)
        if problem == 'corrupt-pages':
            data = source.read_bytes()
            source.write_bytes(data[:len(data) // 2])
        else:
            with closing(sqlite3.connect(source)) as cn:
                if problem == 'foreign-key':
                    cn.execute('PRAGMA foreign_keys=OFF')
                    cn.execute('UPDATE movements SET part_id=999 WHERE id=2')
                elif problem == 'future-schema':
                    cn.execute('PRAGMA user_version=99')
                else:
                    cn.execute('DROP TABLE inspections')
                cn.commit()
    before = digest(source)
    destination = tmp_path / 'rejected-restoration.db'
    with pytest.raises((RuntimeError, sqlite3.Error)):
        server.restore_backup(source, destination)
    assert not destination.exists()
    assert digest(source) == before


@pytest.mark.parametrize('sidecar', ['-wal', '-shm', '-journal'])
def test_restore_rejects_non_standalone_backup_with_auxiliary_file(tmp_path, sidecar):
    source = build_database(tmp_path / 'not-standalone.db')
    Path(str(source) + sidecar).write_bytes(b'auxiliary sqlite file')
    destination = tmp_path / 'must-not-restore.db'
    with pytest.raises(RuntimeError, match='auxiliares'):
        server.restore_backup(source, destination)
    assert not destination.exists()


def test_restore_rejects_wal_format_even_without_sidecars(tmp_path):
    source = build_database(tmp_path / 'wal-format.db')
    with closing(sqlite3.connect(source)) as cn:
        assert cn.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    assert not Path(str(source) + '-wal').exists()
    destination = tmp_path / 'must-not-restore.db'
    with pytest.raises(RuntimeError, match='formato WAL'):
        server.restore_backup(source, destination)
    assert not destination.exists()


def test_restore_refuses_default_fleet_path_even_if_missing(monkeypatch, tmp_path):
    source = build_database(tmp_path / 'backup.db')
    fake_data = tmp_path / 'fake-data'
    fake_data.mkdir()
    monkeypatch.setattr(server, 'DATA', fake_data)
    destination = fake_data / 'fleet.db'
    with pytest.raises(RuntimeError, match='data/fleet.db'):
        server.restore_backup(source, destination)
    assert not destination.exists()


def test_restore_publication_race_cannot_overwrite_other_process_file(monkeypatch, tmp_path):
    source = build_database(tmp_path / 'backup.db')
    destination = tmp_path / 'raced.db'

    def concurrent_creation(staging, target):
        Path(target).write_bytes(b'Newer file from another process')
        raise FileExistsError('Simulated concurrent destination creation')

    monkeypatch.setattr(server.os, 'link', concurrent_creation)
    with pytest.raises(FileExistsError):
        server.restore_backup(source, destination)
    assert destination.read_bytes() == b'Newer file from another process'
    assert not list(tmp_path.glob('.gefleet-snapshot-*'))


def test_restored_idempotency_result_replays_without_duplicate_movement(persisted_client, tmp_path, monkeypatch):
    client, _ = persisted_client
    payload = {'part_id': 1, 'movement_type': 'ENTRADA', 'quantity': '2.125',
               'responsible': 'Bodega', 'reference': 'BACKUP-KEY'}
    headers = {'Idempotency-Key': 'operation-before-backup'}
    first = client.post('/api/movements', json=payload, headers=headers)
    assert first.status_code == 200, first.text
    backup = tmp_path / 'operation-backup.db'
    restored = tmp_path / 'operation-restored.db'
    server.create_backup(backup)
    server.restore_backup(backup, restored)
    before = inspect(restored)
    monkeypatch.setattr(server, 'DB', restored)
    repeated = client.post('/api/movements', json=payload, headers=headers)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == first.json()
    assert inspect(restored) == before


def test_cli_full_backup_restore_and_refusal_procedure(tmp_path):
    active = tmp_path / 'cli-active.db'
    backup = tmp_path / 'cli-backup.db'
    restored = tmp_path / 'cli-restored.db'
    environment = os.environ.copy()
    environment['GEFLEET_DB'] = str(active)
    environment.pop('GEFLEET_ALLOW_CREATE_DB', None)

    def run(*args):
        return subprocess.run([sys.executable, '-B', str(server.ROOT / 'server.py'), *args],
                              env=environment, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=30)

    result = run('db-info')
    assert result.returncode != 0
    assert not active.exists()
    result = run('db-init', '--database', str(active))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['schema_version'] == server.SCHEMA_VERSION
    result = run('db-backup', '--destination', str(backup))
    assert result.returncode == 0, result.stderr
    result = run('db-restore', '--source', str(backup), '--destination', str(restored))
    assert result.returncode == 0, result.stderr
    assert inspect(restored) == inspect(active)
    result = run('db-info', '--database', str(restored))
    assert result.returncode == 0, result.stderr
    info = json.loads(result.stdout)
    assert info['database'] == str(restored.resolve())
    assert info['integrity_check'] == 'ok'
    assert info['foreign_key_check'] == []
    before = digest(restored)
    result = run('db-restore', '--source', str(backup), '--destination', str(restored))
    assert result.returncode != 0
    assert 'No se sobrescribe' in result.stderr
    assert 'Traceback' not in result.stderr
    assert digest(restored) == before


@pytest.mark.parametrize('invalid_environment', ['', 'relative/fleet.db'])
def test_cli_explicit_database_overrides_invalid_environment(tmp_path, invalid_environment):
    target = tmp_path / 'explicit-cli-database.db'
    environment = os.environ.copy()
    environment['GEFLEET_DB'] = invalid_environment
    environment.pop('GEFLEET_ALLOW_CREATE_DB', None)
    command = [sys.executable, '-B', str(server.ROOT / 'server.py')]
    failed = subprocess.run(command + ['db-info'], env=environment,
                            capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=30)
    assert failed.returncode != 0
    assert 'ERROR:' in failed.stderr
    assert 'Traceback' not in failed.stderr
    assert not target.exists()
    created = subprocess.run(command + ['db-init', '--database', str(target)],
                             env=environment, capture_output=True, text=True,
                             encoding='utf-8', errors='replace', timeout=30)
    assert created.returncode == 0, created.stderr
    info = json.loads(created.stdout)
    assert info['database'] == str(target.resolve())
    assert info['schema_version'] == server.SCHEMA_VERSION
