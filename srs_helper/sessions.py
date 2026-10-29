"""Durable session/action registry and immutable result bundles."""
import hashlib
import json
import sqlite3
import os
import shutil
import stat
import portalocker
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
            CREATE TABLE IF NOT EXISTS registry_datasets(path TEXT PRIMARY KEY,name TEXT NOT NULL);
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

    def create(self, dataset_dir, title=None):
        with self.dataset_guard(dataset_dir), open_dataset(dataset_dir) as (_,meta):
            title = title or self.dataset_name(dataset_dir)
            sid = str(uuid4())
            now = datetime.now().isoformat()
            with self.db() as db:
                db.execute("INSERT INTO registry_sessions VALUES (?,?,?,?,?,?,?)",(sid,title,str(Path(dataset_dir).resolve()),meta["dataset_id"],meta["dataset_fingerprint"],now,now))
            self.directory(sid).mkdir(parents=True)
            return sid

    @contextmanager
    def dataset_guard(self, dataset_dir):
        """Shared lifecycle lock for create/run/delete within this workspace registry."""
        locks=self.root/'dataset_locks'
        locks.mkdir(exist_ok=True)
        key=hashlib.sha256(str(Path(dataset_dir).resolve()).casefold().encode()).hexdigest()
        with portalocker.Lock(str(locks/(key+'.lock')),timeout=0):
            yield

    @staticmethod
    def validate_dataset_name(name):
        name=name.strip()
        if not name or len(name)>100 or any(ord(c)<32 for c in name):
            raise ValueError('数据名称须为1～100个字符，不能含换行或控制字符。')
        return name

    def managed_dataset(self, dataset_dir):
        root=self.root/'datasets'
        candidate=Path(dataset_dir).absolute()
        # Reject links/junctions before resolving; recursive deletion never follows them.
        def linked(path):
            return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT) if os.name=='nt' else path.is_symlink()
        if root.exists() and linked(root):
            raise ValueError('数据根目录不能是链接。')
        if candidate.parent.resolve()!=root.resolve() or candidate.name.startswith('.') or linked(candidate):
            raise ValueError('只能管理工作区datasets目录下的完整数据版本。')
        resolved=candidate.resolve()
        if resolved.parent!=root.resolve():
            raise ValueError('数据目录超出管理范围。')
        return resolved

    def dataset_name(self, dataset_dir):
        path=Path(dataset_dir).resolve()
        with self.db() as db:
            row=db.execute('SELECT name FROM registry_datasets WHERE path=?',(str(path),)).fetchone()
        return row[0] if row else '未命名数据 · '+path.name[:12]

    def name_dataset(self, dataset_dir, name):
        name=self.validate_dataset_name(name)
        path=self.managed_dataset(dataset_dir)
        with self.dataset_guard(path), open_dataset(path):
            with self.db() as db:
                db.execute('INSERT OR REPLACE INTO registry_datasets VALUES (?,?)',(str(path),name))
        return name

    def datasets(self):
        result=[]
        for manifest in sorted((self.root/'datasets').glob('*/input_manifest.json')):
            try:
                path=self.managed_dataset(manifest.parent)
                meta=json.loads(manifest.read_text(encoding='utf-8'))
                if not (path/'analysis.duckdb').is_file():
                    continue
                with self.db() as db:
                    count=db.execute('SELECT count(*) FROM registry_sessions WHERE dataset_dir=?',(str(path),)).fetchone()[0]
                result.append({'path':str(path),'name':self.dataset_name(path),
                    'dataset_id':meta['dataset_id'],'status':meta['status'],'sessions':count})
            except (OSError,ValueError,KeyError):
                continue
        return result

    def reload_dataset(self, dataset_dir, *, expected_dataset_id, name, contract=None):
        """Build a new immutable version from verified snapshots; preserve its parent."""
        from .pipeline import build_dataset
        name=self.validate_dataset_name(name)
        path=self.managed_dataset(dataset_dir)
        with self.dataset_guard(path):
            with open_dataset(path) as (_,meta):
                if meta['dataset_id']!=expected_dataset_id:
                    raise ValueError('数据版本已变化，请刷新后重试。')
            files={}
            for entry in meta['files']:
                if not entry.get('sha256') or not entry.get('source_file'):
                    raise ValueError('原版本存在未保存的输入快照，请改用原始CSV加载。')
                if entry['source_file'] in files:
                    raise ValueError('快照清单存在重复路径。')
                files[entry['source_file']]=entry['sha256']
            selected=dict(contract) if contract is not None else dict(meta['contract'])
            if contract is None:
                # Preserve parser settings/aliases; refresh cell semantics from current source.
                selected.pop('field_mapping_file',None)
            snapshot=path/'snapshot'
            if snapshot.resolve()!=snapshot.absolute():
                raise ValueError('快照根目录不能是链接。')
            new_path,new_meta=build_dataset(snapshot,self.root/'datasets',selected,
                snapshot_files=files,reload_of={'dataset_id':meta['dataset_id'],
                    'dataset_fingerprint':meta['dataset_fingerprint'],'source':'VERIFIED_SNAPSHOT'})
            if new_meta['status']!='FAILED':
                self.name_dataset(new_path,name)
            return new_path,new_meta

    def delete_dataset(self, dataset_dir, *, expected_dataset_id):
        path=self.managed_dataset(dataset_dir)
        with self.dataset_guard(path):
            meta=json.loads((path/'input_manifest.json').read_text(encoding='utf-8'))
            if meta['dataset_id']!=expected_dataset_id:
                raise ValueError('数据版本已变化，请刷新列表后重试。')
            UUID(meta['dataset_id'])
            for base,dirs,files in os.walk(path,followlinks=False):
                for name in dirs+files:
                    entry=Path(base)/name
                    linked=bool(entry.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT) if os.name=='nt' else entry.is_symlink()
                    if linked or not entry.resolve().is_relative_to(path):
                        raise ValueError('数据目录包含链接或越界路径，拒绝删除。')
            # Validate the final absolute target immediately before recursive deletion.
            if path.resolve().parent!=(self.root/'datasets').resolve():
                raise ValueError('删除目标超出数据目录。')
            shutil.rmtree(path)
        # Keep the display name for historical sessions; datasets() only lists existing files.
        # Historical sessions and their immutable reports/artifacts intentionally remain.

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
