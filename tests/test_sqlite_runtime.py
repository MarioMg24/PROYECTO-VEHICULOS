"""Runtime/WAL compatibility checks. All databases and snapshots are temporary.

These checks exercise concurrent writes/checkpoints; they do not reproduce the
rare WAL-reset race deterministically or prove a SQLite build is free of bugs.
The opt-in version requirement follows https://sqlite.org/wal.html#walreset.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
from threading import Barrier, Event

import server


def fixed_wal_reset_version(version):
    """Recognize corrected upstream releases and their documented backports."""
    return (
        (version >= (3, 51, 3) and version != (3, 52, 0))
        or (version[:2] == (3, 44) and version[2] >= 6)
        or (version[:2] == (3, 50) and version[2] >= 7)
    )


def test_runtime_binding_reports_the_sqlite_library_actually_loaded(record_property):
    with closing(sqlite3.connect(':memory:')) as cn:
        sql_version, source_id = cn.execute(
            'SELECT sqlite_version(), sqlite_source_id()'
        ).fetchone()
    assert sql_version == sqlite3.sqlite_version
    assert tuple(map(int, sql_version.split('.'))) == sqlite3.sqlite_version_info
    assert sqlite3.threadsafety >= 1
    assert len(source_id.split()[-1]) == 64
    record_property('python', sys.version)
    record_property('python_executable', sys.executable)
    record_property('sqlite', sql_version)
    record_property('sqlite_source_id', source_id)


def test_corrected_sqlite_is_required_when_runtime_validation_is_opted_in():
    # Existing .venv remains usable for rollback. Adoption checks explicitly set
    # this variable; pip packages cannot change the bundled SQLite version.
    if os.environ.get('GEFLEET_REQUIRE_FIXED_SQLITE') == '1':
        with closing(sqlite3.connect(':memory:')) as cn:
            sql_version = cn.execute('SELECT sqlite_version()').fetchone()[0]
        assert sql_version == sqlite3.sqlite_version
        assert fixed_wal_reset_version(sqlite3.sqlite_version_info), (
            f'SQLite {sql_version} does not meet the documented WAL-reset fix '
            'requirement. Use an official corrected Python runtime.'
        )


def _inventory_snapshot(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as cn:
        assert cn.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert cn.execute('PRAGMA foreign_key_check').fetchall() == []
        assert cn.execute('PRAGMA user_version').fetchone()[0] == 2
        return {
            'movements': cn.execute('SELECT * FROM movements ORDER BY id').fetchall(),
            'operations': cn.execute(
                'SELECT * FROM inventory_operations ORDER BY operation_key'
            ).fetchall(),
            'vehicles': cn.execute('SELECT * FROM vehicles ORDER BY id').fetchall(),
            'stock': server.stock(cn, 1),
        }


def test_concurrent_wal_checkpoints_preserve_atomic_idempotent_inventory_and_backup(
        monkeypatch, tmp_path):
    database = tmp_path / 'runtime-wal.db'
    monkeypatch.setattr(server, 'DB', database)
    server.init_db(allow_create=True)
    start = Barrier(3)
    first_checkpoint = Event()
    finished = Event()
    writes_per_thread = 100

    # Keep one connection open so the last writer does not remove the WAL before
    # the committed-WAL snapshot check. This handle stays in the main thread.
    with closing(sqlite3.connect(database, isolation_level=None)) as keeper:
        keeper.execute('PRAGMA foreign_keys=ON')
        assert keeper.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        keeper.execute('PRAGMA wal_autocheckpoint=0')
        keeper.execute("INSERT INTO vehicles(id,plate,name) VALUES(1,'TEMP-WAL','Temporary vehicle')")
        keeper.execute("INSERT INTO parts(id,code,name,unit) VALUES(1,'TEMP-WAL','Temporary part','unidad')")

        def write(worker):
            start.wait(timeout=10)
            assert first_checkpoint.wait(timeout=10)
            ids = []
            for sequence in range(writes_per_thread):
                key = f'runtime-wal-{worker}-{sequence}'
                payload = server.MoveIn(
                    part_id=1, movement_type='ENTRADA', quantity='1',
                    responsible='Runtime test', reference=key,
                )
                first = server.create_movement(payload, idempotency_key=key)
                replay = server.create_movement(payload, idempotency_key=key)
                assert replay == first
                ids.append(first['id'])
            return ids

        def checkpoint():
            results = []
            with closing(sqlite3.connect(database, timeout=15,
                                         isolation_level=None)) as cn:
                assert cn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
                # A busy checkpoint is an expected outcome, not a lost write.
                # Zero checkpoint busy timeout avoids monopolizing the writer.
                cn.execute('PRAGMA busy_timeout=0')
                start.wait(timeout=10)
                modes = ('PASSIVE', 'RESTART', 'TRUNCATE')
                while not finished.is_set():
                    mode = modes[len(results) % len(modes)]
                    result = cn.execute(f'PRAGMA wal_checkpoint({mode})').fetchone()
                    assert result[0] in (0, 1)
                    results.append(result)
                    first_checkpoint.set()
                    finished.wait(0.001)
            return results

        with ThreadPoolExecutor(max_workers=3) as pool:
            checkpointer = pool.submit(checkpoint)
            writers = [pool.submit(write, worker) for worker in range(2)]
            try:
                writer_results = [future.result(timeout=30) for future in writers]
            finally:
                finished.set()
            checkpoint_results = checkpointer.result(timeout=20)

        assert checkpoint_results
        assert len(set(writer_results[0] + writer_results[1])) == writes_per_thread * 2
        # Guarantee a committed tail only in WAL while the backup is produced.
        keeper.execute("UPDATE vehicles SET notes='Committed WAL tail' WHERE id=1")
        assert Path(str(database) + '-wal').stat().st_size > 0
        expected = _inventory_snapshot(database)
        assert len(expected['movements']) == writes_per_thread * 2
        assert len(expected['operations']) == writes_per_thread * 2
        assert expected['stock'] == writes_per_thread * 2 * 1000

        backup = tmp_path / 'runtime-wal-backup.db'
        restored = tmp_path / 'runtime-wal-restored.db'
        backup_info = server.create_backup(backup)
        restore_info = server.restore_backup(backup, restored)
        assert backup_info['journal_mode'] == 'delete'
        assert restore_info['journal_mode'] == 'delete'
        assert backup_info['sqlite_version'] == sqlite3.sqlite_version
        assert restore_info['sqlite_version'] == sqlite3.sqlite_version
        assert _inventory_snapshot(backup) == expected
        assert _inventory_snapshot(restored) == expected
        assert _inventory_snapshot(database) == expected

    # Reopen after every WAL connection has closed to verify durable contents,
    # rather than checking only a live connection's in-memory view.
    assert _inventory_snapshot(database) == expected
