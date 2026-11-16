"""节点本机持久化 inbox/outbox；状态目录必须由单一进程独占。"""
import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path


class NodeState:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.closed = False
        self._file = open(self.path / 'worker.lock', 'a+b')
        try:
            self._file.seek(0)
            if os.fstat(self._file.fileno()).st_size == 0:
                self._file.write(b'0'); self._file.flush()
            self._file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._file.close()
            raise RuntimeError('状态目录已由另一执行进程占用') from None
        try:
            self.db = sqlite3.connect(self.path / 'node.db', check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS dispatches(
                    dispatch_id TEXT PRIMARY KEY, task_json TEXT NOT NULL, status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                    event_json TEXT NOT NULL, error TEXT NOT NULL DEFAULT '');
            ''')
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO metadata VALUES (?,?)',
                                ('instance_id', uuid.uuid4().hex))
            self.instance_id = self.db.execute("SELECT value FROM metadata WHERE key='instance_id'").fetchone()[0]
        except BaseException:
            self.close()
            raise

    def bind(self, node_id, machine_id):
        with self.lock, self.db:
            identity = json.dumps([node_id, machine_id])
            old = self.db.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()
            if old and old[0] != identity:
                raise ValueError('状态目录已绑定其他节点或机器')
            self.db.execute('INSERT OR IGNORE INTO metadata VALUES (?,?)', ('identity', identity))

    def accept(self, task):
        if task.get('instance_id') != self.instance_id:
            raise ValueError('任务实例不匹配')
        for key in ('dispatch_id', 'task_id'):
            if not isinstance(task.get(key), str) or not task[key]:
                raise ValueError('任务缺少分配标识')
        with self.lock, self.db:
            old = self.db.execute('SELECT task_json FROM dispatches WHERE dispatch_id=?',
                                  (task['dispatch_id'],)).fetchone()
            if old:
                if json.loads(old[0])['task_id'] != task['task_id']:
                    raise ValueError('分配标识冲突')
                return False
            self.db.execute('INSERT INTO dispatches VALUES (?,?,?)',
                            (task['dispatch_id'], json.dumps(task), 'queued'))
            return True

    def queued(self):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute(
                "SELECT task_json FROM dispatches WHERE status='queued' ORDER BY rowid")]

    def active_ids(self):
        with self.lock:
            return [r[0] for r in self.db.execute("SELECT dispatch_id FROM dispatches WHERE status!='completed'")]

    def mark_running(self, dispatch_id):
        with self.lock, self.db:
            return self.db.execute("UPDATE dispatches SET status='running' WHERE dispatch_id=? AND status='queued'",
                                   (dispatch_id,)).rowcount == 1

    def _event(self, dispatch_id, kind, payload):
        row = self.db.execute('SELECT task_json FROM dispatches WHERE dispatch_id=?', (dispatch_id,)).fetchone()
        if row is None:
            raise ValueError('未知分配标识')
        task = json.loads(row[0])
        event = dict(event_id=uuid.uuid4().hex, type=kind, task_id=task['task_id'],
                     dispatch_id=dispatch_id, payload=payload)
        self.db.execute('INSERT INTO events(event_id,event_json) VALUES (?,?)',
                        (event['event_id'], json.dumps(event)))

    def logs(self, dispatch_id, logs):
        with self.lock, self.db:
            self._event(dispatch_id, 'logs', {'logs': logs})

    def finish(self, dispatch_id, result):
        with self.lock, self.db:
            row = self.db.execute('SELECT status FROM dispatches WHERE dispatch_id=?', (dispatch_id,)).fetchone()
            if not row or row[0] == 'completed':
                return
            self._event(dispatch_id, 'result', result)
            # 清除已完成任务内可能包含的 Profile 登录态，保留执行去重标识。
            task = json.loads(self.db.execute('SELECT task_json FROM dispatches WHERE dispatch_id=?', (dispatch_id,)).fetchone()[0])
            self.db.execute("UPDATE dispatches SET status='completed',task_json=? WHERE dispatch_id=?",
                            (json.dumps({k: task[k] for k in ('task_id', 'dispatch_id', 'instance_id')}), dispatch_id))

    def recover(self):
        with self.lock:
            interrupted = [r[0] for r in self.db.execute("SELECT dispatch_id FROM dispatches WHERE status='running'")]
            for did in interrupted:
                self.finish(did, dict(ok=False, retryable=False, steps=[], elapsed_ms=0,
                                      error='执行节点重启，执行状态未知'))

    def events(self):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute('SELECT event_json FROM events ORDER BY seq')]

    def acknowledge(self, event_id):
        with self.lock, self.db:
            self.db.execute('DELETE FROM events WHERE event_id=?', (event_id,))

    def diagnostic(self, message, event_id=None):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', ('last_error', message))
            if event_id:
                self.db.execute('UPDATE events SET error=? WHERE event_id=?', (message, event_id))

    def restrict_to_uploads(self):
        """已确认身份被替换；重启也不能夺回活动实例或领取新任务。"""
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', ('upload_only', 'true'))

    def status(self):
        with self.lock:
            error = self.db.execute("SELECT value FROM metadata WHERE key='last_error'").fetchone()
            upload_only = self.db.execute("SELECT value FROM metadata WHERE key='upload_only'").fetchone()
            return dict(instance_id=self.instance_id,
                        upload_only=bool(upload_only),
                        dispatches=dict(self.db.execute('SELECT status,COUNT(*) FROM dispatches GROUP BY status').fetchall()),
                        pending_events=self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],
                        last_error=error[0] if error else '')

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if hasattr(self, 'db'):
                self.db.close()
            if not self._file.closed:
                self._file.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
                self._file.close()
