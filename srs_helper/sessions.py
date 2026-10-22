"""Durable session/action registry and immutable result bundles."""
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from .database import open_dataset


def dumps(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,default=str)


class SessionStore:
    def __init__(self, root="workspace_storage"):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.db_path = self.root/"session_registry.sqlite"
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS registry_sessions(id TEXT PRIMARY KEY,title TEXT,dataset_dir TEXT,dataset_id TEXT,fingerprint TEXT,created TEXT,accessed TEXT);
            CREATE TABLE IF NOT EXISTS registry_actions(session_id TEXT,action_id TEXT,request_hash TEXT,status TEXT,result_path TEXT,error TEXT,result_hash TEXT,PRIMARY KEY(session_id,action_id));
            CREATE TABLE IF NOT EXISTS registry_ui(session_id TEXT,key TEXT,value TEXT,PRIMARY KEY(session_id,key));
            """)

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.db_path,timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create(self, dataset_dir, title="新诊断"):
        with open_dataset(dataset_dir) as (_,meta):
            sid = str(uuid4())
            now = datetime.now().isoformat()
            with self.db() as db:
                db.execute("INSERT INTO registry_sessions VALUES (?,?,?,?,?,?,?)",(sid,title,str(Path(dataset_dir).resolve()),meta["dataset_id"],meta["dataset_fingerprint"],now,now))
            self.directory(sid).mkdir(parents=True)
            return sid

    def directory(self, sid):
        return self.root/"sessions"/str(UUID(sid))

    def get(self,sid):
        with self.db() as db:
            row = db.execute("SELECT * FROM registry_sessions WHERE id=?",(sid,)).fetchone()
            if row is None:
                raise ValueError("Session not found")
            return dict(row)

    def list(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM registry_sessions ORDER BY accessed DESC")]

    def ui_value(self,sid,key,default=None):
        with self.db() as db:
            row = db.execute("SELECT value FROM registry_ui WHERE session_id=? AND key=?",(sid,key)).fetchone()
        return json.loads(row[0]) if row else default

    def set_ui_value(self,sid,key,value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO registry_ui VALUES (?,?,?)",(sid,key,dumps(value)))

    def begin(self,sid,action_id,request,retry=False):
        UUID(action_id)
        digest = hashlib.sha256(dumps(request).encode()).hexdigest()
        with self.db() as db:
            row = db.execute("SELECT * FROM registry_actions WHERE session_id=? AND action_id=?",(sid,action_id)).fetchone()
            if row:
                if row["request_hash"]!=digest:
                    raise ValueError("Action ID was reused for a different request")
                if row["status"]=="DONE":
                    return self.read_result(dict(row))
                if not retry:
                    raise ValueError("Previous action is incomplete; explicitly retry to resume")
            db.execute("INSERT OR REPLACE INTO registry_actions VALUES (?,?,?,?,?,?,?)",(sid,action_id,digest,"RUNNING",None,None,None))
        return None

    def finish(self,sid,action_id,result):
        path = self.directory(sid)/"artifacts"/(str(uuid4())+".json")
        path.parent.mkdir(parents=True,exist_ok=True)
        content = dumps(result).encode()
        with path.open("xb") as stream:
            stream.write(content)
        with self.db() as db:
            db.execute("UPDATE registry_actions SET status='DONE',result_path=?,result_hash=?,error=NULL WHERE session_id=? AND action_id=?",(str(path),hashlib.sha256(content).hexdigest(),sid,action_id))
            db.execute("UPDATE registry_sessions SET accessed=? WHERE id=?",(datetime.now().isoformat(),sid))

    def fail(self,sid,action_id):
        with self.db() as db:
            db.execute("UPDATE registry_actions SET status='FAILED',error='Action failed; retry available' WHERE session_id=? AND action_id=?",(sid,action_id))

    def read_result(self,record):
        path = Path(record["result_path"])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=record["result_hash"]:
            raise ValueError("Saved response is missing or modified")
        return json.loads(path.read_text(encoding="utf-8"))

    def history(self,sid):
        with self.db() as db:
            records = [dict(r) for r in db.execute("SELECT * FROM registry_actions WHERE session_id=? ORDER BY rowid",(sid,))]
        result = []
        for record in records:
            if record["status"]=="DONE":
                try:
                    response = self.read_result(record)
                    from .evidence import verify_artifact
                    response["missing_artifacts"] = []
                    for artifact in response.get("artifacts",[]):
                        try:
                            verify_artifact(artifact)
                        except (ValueError,OSError):
                            response["missing_artifacts"].append(artifact["artifact_id"])
                    result.append(response)
                except ValueError:
                    result.append({"action_id":record["action_id"],"status":"MISSING_RESULT"})
            else:
                result.append({"action_id":record["action_id"],"status":record["status"]})
        return result
