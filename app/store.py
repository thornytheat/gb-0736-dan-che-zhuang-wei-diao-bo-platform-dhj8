import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .errors import DomainError
from .manifest import canonical

SCHEMA = '''
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    run_id TEXT NOT NULL REFERENCES runs(id),
    name TEXT NOT NULL,
    raw BLOB NOT NULL,
    PRIMARY KEY (run_id, name)
);
CREATE TABLE IF NOT EXISTS active (
    slot INTEGER PRIMARY KEY CHECK (slot = 1),
    run_id TEXT NOT NULL REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS faults (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    received_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS diversions (
    fault_id TEXT PRIMARY KEY,
    source_run TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(id),
    option TEXT NOT NULL
);
'''


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.execute('PRAGMA foreign_keys = ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def active(self):
        with self.connect() as db:
            row = db.execute('SELECT run_id FROM active WHERE slot = 1').fetchone()
        return row[0] if row else None

    def load(self, run_id=None):
        with self.connect() as db:
            if run_id is None:
                row = db.execute('SELECT payload FROM runs JOIN active ON runs.id = active.run_id').fetchone()
            else:
                row = db.execute('SELECT payload FROM runs WHERE id = ?', (run_id,)).fetchone()
        if row is None:
            raise DomainError('RUN_NOT_FOUND', 404)
        return json.loads(row[0])

    def sources(self, run_id):
        with self.connect() as db:
            return dict(db.execute('SELECT name, raw FROM sources WHERE run_id = ?', (run_id,)))

    def history(self):
        sql = '''SELECT runs.id, generated_at, json_extract(payload, '$.shift.name'),
                        json_extract(payload, '$.manifest.inputs.events.sha256'),
                        json_extract(payload, '$.dispatch.comparison.delta'), active.run_id IS NOT NULL,
                        json_extract(payload, '$.divert') IS NOT NULL
                 FROM runs LEFT JOIN active ON runs.id = active.run_id
                 ORDER BY generated_at DESC, runs.id'''
        keys = ('id', 'generated_at', 'shift', 'source_sha256', 'delta', 'active', 'diverted')
        with self.connect() as db:
            return [dict(zip(keys, row)) for row in db.execute(sql)]

    def faults(self):
        with self.connect() as db:
            rows = db.execute('SELECT id, run_id, received_at, payload FROM faults ORDER BY received_at, id')
            return [{'id': row[0], 'run_id': row[1], 'received_at': row[2], 'report': json.loads(row[3])}
                    for row in rows]

    def fault(self, fault_id):
        with self.connect() as db:
            row = db.execute('SELECT id, run_id, received_at, payload FROM faults WHERE id = ?',
                             (fault_id,)).fetchone()
        if row is None:
            raise DomainError('FAULT_NOT_FOUND', 404)
        return {'id': row[0], 'run_id': row[1], 'received_at': row[2], 'report': json.loads(row[3])}

    def diversion(self, fault_id):
        with self.connect() as db:
            row = db.execute('SELECT source_run, run_id, option FROM diversions WHERE fault_id = ?',
                             (fault_id,)).fetchone()
        if row is None:
            return None
        return {'source_run': row[0], 'run_id': row[1], 'option': row[2]}

    def publish_diversion(self, snapshot, sources, fault, expected):
        manifest = snapshot['manifest']
        run_id = manifest['id']
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT run_id FROM active WHERE slot = 1').fetchone()
            active = row[0] if row else None
            existing = db.execute('SELECT payload FROM runs WHERE id = ?', (run_id,)).fetchone()
            record = db.execute('SELECT run_id, source_run, option FROM diversions WHERE fault_id = ?',
                                (fault['id'],)).fetchone()
            if record is not None:
                if (record[0], record[1], record[2]) != (run_id, expected, fault['option']):
                    raise DomainError('DUPLICATE_FAULT', 409, fault=fault['id'], published=record[0])
                if existing is not None:
                    stored = json.loads(existing[0])
                    comparable = dict(snapshot, manifest=dict(manifest))
                    for key in ('generated_at', 'elapsed_ms'):
                        comparable['manifest'][key] = stored['manifest'][key]
                    if stored != comparable:
                        raise DomainError('REPLAY_DRIFT', 409)
                if active not in (run_id, expected):
                    raise DomainError('STALE_RUN', 409, current=active)
                if active == expected:
                    db.execute('UPDATE active SET run_id = ? WHERE slot = 1', (run_id,))
                return {'created': False, 'reused': True}
            if active != expected:
                raise DomainError('STALE_RUN', 409, current=active)
            if existing is None:
                db.execute('INSERT INTO runs VALUES (?, ?, ?)',
                           (run_id, manifest['generated_at'], canonical(snapshot)))
                db.executemany('INSERT INTO sources VALUES (?, ?, ?)',
                               [(run_id, name, raw) for name, raw in sources.items()])
            db.execute('INSERT INTO faults VALUES (?, ?, ?, ?) ON CONFLICT(id) DO NOTHING',
                       (fault['id'], expected, fault['received_at'], canonical(fault['report'])))
            db.execute('INSERT INTO diversions VALUES (?, ?, ?, ?)',
                       (fault['id'], expected, run_id, fault['option']))
            db.execute('INSERT INTO active VALUES (1, ?) ON CONFLICT(slot) DO UPDATE SET run_id = excluded.run_id',
                       (run_id,))
            return {'created': existing is None, 'reused': False}

    def reuse_diversion(self, fault_id, expected):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT run_id, source_run, option FROM diversions WHERE fault_id = ?',
                             (fault_id,)).fetchone()
            if row is None:
                raise DomainError('FAULT_NOT_FOUND', 404)
            run_id, source_run, option = row
            active = db.execute('SELECT run_id FROM active WHERE slot = 1').fetchone()
            active = active[0] if active else None
            # 只允许在原方案或已改派班次上重试；在更新的班次上重试会让旧改派回退当前版本。
            if expected not in (run_id, source_run):
                raise DomainError('DUPLICATE_FAULT', 409, fault=fault_id, published=run_id)
            if active not in (run_id, expected):
                raise DomainError('STALE_RUN', 409, current=active)
            if active == expected:
                db.execute('UPDATE active SET run_id = ? WHERE slot = 1', (run_id,))
            return {'run_id': run_id, 'source_run': source_run, 'option': option}

    def publish(self, snapshot, sources, expected):
        manifest = snapshot['manifest']
        run_id = manifest['id']
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT run_id FROM active WHERE slot = 1').fetchone()
            active = row[0] if row else None
            existing = db.execute('SELECT payload FROM runs WHERE id = ?', (run_id,)).fetchone()
            if existing is not None:
                stored = json.loads(existing[0])
                comparable = dict(snapshot, manifest=dict(manifest))
                for key in ('generated_at', 'elapsed_ms'):
                    comparable['manifest'][key] = stored['manifest'][key]
                if stored != comparable:
                    raise DomainError('REPLAY_DRIFT', 409)
                if active == run_id:
                    return False
            if active != expected:
                raise DomainError('STALE_RUN', 409, current=active)
            if existing is None:
                db.execute('INSERT INTO runs VALUES (?, ?, ?)',
                           (run_id, manifest['generated_at'], canonical(snapshot)))
                db.executemany('INSERT INTO sources VALUES (?, ?, ?)',
                               [(run_id, name, raw) for name, raw in sources.items()])
            db.execute('INSERT INTO active VALUES (1, ?) ON CONFLICT(slot) DO UPDATE SET run_id = excluded.run_id',
                       (run_id,))
            return existing is None
