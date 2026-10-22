"""Human-reviewed, immutable Markdown case revisions with retryable indexing."""
import json
import hashlib
import os
import shutil
from pathlib import Path
from uuid import uuid4

import yaml
import duckdb

from .evidence import verify_artifact
from .knowledge import CaseCard, parse_document, safe_asset


def markdown(metadata,body):
    return "---\n"+yaml.safe_dump(metadata,allow_unicode=True,sort_keys=False)+"---\n\n"+body


def create_draft(store,sid,result):
    case_id = "CASE_"+uuid4().hex
    folder = store.directory(sid)/"drafts"/case_id
    folder.mkdir(parents=True)
    metadata = CaseCard(case_id=case_id,title=result["question"][:120],source_refs=[{
        "document_id":result["dataset_id"],"revision":result["dataset_fingerprint"],"section":"action:"+result["action_id"]}],
        applicability="待人工补充版本、场景和适用条件").model_dump(mode="json")
    body = ("# 观察\n\n"+result["answer"]+"\n\n# 相似点、差异与未验证项\n\n待人工补充。\n\n"
            "# 排查过程\n\n待人工核对查询结果、补充外部证据。\n\n# 根因状态\n\n未知；本草稿不确认根因。\n")
    chart = result.get("selected_chart") or result.get("visual",{}).get("current_chart")
    if chart and chart.get("status")=="READY":
        image = Path(chart["png"])
        if not image.is_file() or hashlib.sha256(image.read_bytes()).hexdigest()!=chart["sha256"]:
            raise ValueError("Selected chart is missing or modified")
        if chart["dataset_fingerprint"]!=result["dataset_fingerprint"] or chart["scope"]!=result["scope"]:
            raise ValueError("Selected chart belongs to another data scope")
        (folder/"images").mkdir()
        shutil.copyfile(image,folder/"images/current_gain.png")
        body += "\n\n![当前数据流增益均值；仅预览活跃对象，精确统计以完整表为准](images/current_gain.png)\n"
    (folder/(case_id+".md")).write_text(markdown(metadata,body),encoding="utf-8")
    evidence = []
    for artifact in result.get("artifacts",[]):
        verify_artifact(artifact)
        evidence.append({k:v for k,v in artifact.items() if k!="preview"})
    (folder/"evidence.json").write_text(json.dumps(evidence,ensure_ascii=False,default=str,indent=2),encoding="utf-8")
    (folder/"replay.json").write_text(json.dumps({"dataset_dir":store.get(sid)["dataset_dir"],"scope":result["scope"],
        "plan":result["plan"],"feature":result.get("feature"),"artifacts":evidence},ensure_ascii=False,default=str,indent=2),encoding="utf-8")
    return folder


def replay_queries(draft_folder):
    from .agent import restore_scope
    from .charts import time_series_artifact
    from .database import open_dataset
    from .evidence import feature_analysis
    from .plans import QueryPlan, execute_plan
    from .sql_executor import execute_sql
    path = Path(draft_folder)/"replay.json"
    if not path.exists():
        return  # Text-only cases need no executable assertions.
    bundle = json.loads(path.read_text(encoding="utf-8"))
    directory,scope = bundle["dataset_dir"],restore_scope(bundle["scope"])
    output = Path(draft_folder)/"replay_results"/uuid4().hex
    expected = bundle["artifacts"]
    exploratory = [a for a in expected if a["operator"]=="EXPLORATION_SQL"]
    if exploratory:
        actual = execute_sql(directory,scope,exploratory[0]["sql"],exploratory[0]["parameters"],output)
    elif any(a["operator"]!="TIME_SERIES" and a.get("origin")!="FEATURE" for a in expected):
        actual = execute_plan(directory,scope,QueryPlan.model_validate(bundle["plan"]),output)
    else:
        actual = []
    if any(a["operator"]=="TIME_SERIES" for a in expected):
        actual.append(time_series_artifact(directory,scope,output))
    if bundle.get("feature"):
        with open_dataset(directory) as (_,meta):
            feature = feature_analysis(directory,scope,meta,bundle["feature"]["feature"],output)
        actual.extend(feature["artifacts"])
    if len(actual)!=len(expected):
        raise ValueError("Replay produced a different set of evidence results")
    for old,new in zip(expected,actual):
        verify_artifact(old)
        if old["dataset_fingerprint"]!=new["dataset_fingerprint"] or old["row_count"]!=new["row_count"]:
            raise ValueError("Replay scope or result count differs from the original evidence")
        # Trusted internal artifact comparison, not a model/manual SQL entry point.
        with duckdb.connect(config={"memory_limit":"512MB","threads":"2"}) as con:
            difference = con.execute("""SELECT count(*) FROM (
                (SELECT * FROM read_parquet(?) EXCEPT ALL SELECT * FROM read_parquet(?)) UNION ALL
                (SELECT * FROM read_parquet(?) EXCEPT ALL SELECT * FROM read_parquet(?)))""",
                [old["path"],new["path"],new["path"],old["path"]]).fetchone()[0]
        if difference:
            raise ValueError("Replay values differ from the original evidence")


def publish_case(index,draft_file,reviewer,approved=False):
    if not approved or not reviewer.strip():
        raise ValueError("Explicit human approval and reviewer identity required")
    draft_file = Path(draft_file)
    doc = parse_document(draft_file,"case")
    metadata = doc["metadata"]
    if not metadata["source_refs"] or not metadata["applicability"].strip():
        raise ValueError("Case source and applicability are required")
    if doc["warnings"]:
        raise ValueError("Fix missing/invalid images before publishing")
    if metadata["root_cause_status"]=="CONFIRMED" and not metadata.get("confirmation_evidence"):
        raise ValueError("Confirmed root cause requires confirmation_evidence")
    evidence_file = draft_file.parent/"evidence.json"
    if evidence_file.exists():
        for artifact in json.loads(evidence_file.read_text(encoding="utf-8")):
            verify_artifact(artifact)
    replay_queries(draft_file.parent)
    metadata.update(review_status="REVIEWED",reviewer=reviewer)
    content = markdown(metadata,doc["body"])
    parent = index.assets/"cases"/metadata["case_id"]
    parent.mkdir(parents=True,exist_ok=True)
    if not parent.resolve().is_relative_to(index.assets):
        raise ValueError("Case output escapes asset root")
    target = parent/f"r{metadata['revision']:04d}"
    destination = target/(metadata["case_id"]+".md")
    if target.exists():
        if not destination.is_file() or destination.read_text(encoding="utf-8")!=content:
            raise ValueError("Revision already exists; increment revision to publish changes")
        # Idempotent retry must also bind image content, not just Markdown.
        existing = parse_document(destination,"case")
        if existing["images"] != doc["images"]:
            raise ValueError("Image content changed; increment revision")
    else:
        pending = parent/(".pending-"+uuid4().hex)
        pending.mkdir()
        (pending/destination.name).write_text(content,encoding="utf-8")
        for image in doc["images"]:
            dst = safe_asset(pending,image["path"])
            dst.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(safe_asset(draft_file.parent,image["path"]),dst)
        if evidence_file.exists():
            shutil.copyfile(evidence_file,pending/"evidence.json")
        pending.rename(target)
    status = "PENDING_INDEX"
    try:
        results = index.sync()
        key = destination.relative_to(index.assets).as_posix()
        if any(r["path"]==key and r["status"] in {"UPDATED","UNCHANGED"} for r in results):
            status = "READY"
    except Exception:
        pass  # authority files are safely published; index can be rebuilt later
    with index.connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS reviews(case_id TEXT,revision INTEGER,reviewer TEXT,status TEXT,path TEXT,PRIMARY KEY(case_id,revision))")
        db.execute("INSERT OR REPLACE INTO reviews VALUES (?,?,?,?,?)",(metadata["case_id"],metadata["revision"],reviewer,status,str(destination)))
    return {"path":str(destination),"status":status,"case_id":metadata["case_id"],"revision":metadata["revision"]}
