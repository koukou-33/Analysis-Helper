"""Action recovery state: one transactional authority, no replaceable JSON journal."""
import hashlib
import json
import sqlite3
import time
from contextlib import closing

from .sessions import dumps
from .performance import validation_event


class StorageFailure(RuntimeError):
    code = 'STORAGE_FAILURE'


class ActionProgress:
    def __init__(self, store, sid, action_id, folder):
        self.store, self.sid, self.action_id = store, sid, action_id
        self.revision = 0
        self.data = {}
        with store.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS action_progress (
                session_id TEXT, action_id TEXT, request_hash TEXT NOT NULL,
                revision INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(session_id,action_id))''')
            action = db.execute('SELECT request_hash FROM registry_actions WHERE session_id=? AND action_id=?',
                                (sid, action_id)).fetchone()
            if action is None:
                raise StorageFailure('STORAGE_FAILURE：动作尚未注册')
            self.request_hash = action[0]
            row = db.execute('SELECT * FROM action_progress WHERE session_id=? AND action_id=?',
                             (sid, action_id)).fetchone()
        if row:
            if row['request_hash'] != self.request_hash:
                raise StorageFailure('STORAGE_FAILURE：进度与请求不匹配')
            try:
                self.data = json.loads(row['payload'])
                if not isinstance(self.data, dict): raise ValueError('Invalid progress')
            except ValueError as exc:
                raise StorageFailure('STORAGE_FAILURE：进度损坏，未重置预算') from exc
            self.revision = row['revision']
        else:
            legacy = folder / ('progress_' + hashlib.sha256(action_id.encode()).hexdigest() + '.json')
            if legacy.exists():
                try:
                    data = json.loads(legacy.read_text(encoding='utf-8'))
                    if not isinstance(data, dict): raise ValueError('Invalid legacy progress')
                    from .evidence import verify_artifact
                    for artifact in data.get('artifacts', []): verify_artifact(artifact)
                    self.save(**data)
                except (ValueError, OSError, KeyError) as exc:
                    raise StorageFailure('STORAGE_FAILURE：旧进度不可恢复，未重置预算') from exc

    def save(self, **updates):
        candidate = {**self.data, **updates}
        payload = dumps(candidate)
        for attempt in range(3):
            try:
                with closing(sqlite3.connect(self.store.db_path, timeout=0.15)) as db, db:
                    db.execute('BEGIN IMMEDIATE')
                    action = db.execute('SELECT request_hash FROM registry_actions WHERE session_id=? AND action_id=?',
                                        (self.sid, self.action_id)).fetchone()
                    if action is None or action[0] != self.request_hash:
                        raise StorageFailure('STORAGE_FAILURE：动作请求已变化')
                    row = db.execute('SELECT revision FROM action_progress WHERE session_id=? AND action_id=?',
                                     (self.sid, self.action_id)).fetchone()
                    if (row[0] if row else 0) != self.revision:
                        raise StorageFailure('STORAGE_FAILURE：动作进度存在并发更新')
                    db.execute('INSERT OR REPLACE INTO action_progress VALUES (?,?,?,?,?)',
                               (self.sid, self.action_id, self.request_hash, self.revision+1, payload))
                self.revision += 1
                # Retain the shared dict reference used by the running graph.
                self.data.update(updates)
                return
            except sqlite3.Error as exc:
                code = getattr(exc, 'sqlite_errorcode', None)
                transient = code is not None and (code & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                validation_event('progress_storage', attempt+1, {
                    'operation':'SQLITE_COMMIT', 'path_type':'SESSION_REGISTRY',
                    'sqlite_errorcode':code, 'errno':getattr(exc,'errno',None),
                    'winerror':getattr(exc,'winerror',None), 'retryable':transient})
                if not transient or attempt == 2:
                    raise StorageFailure('STORAGE_FAILURE：无法保存恢复进度；已有证据保留') from exc
                time.sleep(0.05 * (attempt+1))
