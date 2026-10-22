import argparse
import json
from pathlib import Path
from datetime import datetime

import duckdb

from .pipeline import build_dataset
from .synthetic import generate
from .queries import Scope, summarize


def main():
    parser = argparse.ArgumentParser(description="SRS deterministic data foundation")
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("load")
    load.add_argument("input_dir", type=Path)
    load.add_argument("--storage", type=Path, default=Path("workspace_storage/datasets"))
    load.add_argument("--contract", type=Path, default=Path("config/input_contract.json"))
    inspect = sub.add_parser("inspect")
    inspect.add_argument("dataset_dir", type=Path)
    synthetic = sub.add_parser("generate", help="Generate synthetic CSVs from template header only")
    synthetic.add_argument("output", type=Path)
    synthetic.add_argument("--template", type=Path, default=Path("docs/NR_SRS_temp.csv"))
    synthetic.add_argument("--seed", type=int, default=20261022)
    synthetic.add_argument("--blocks", type=int, default=50)
    synthetic.add_argument("--files", type=int, default=2)
    synthetic.add_argument("--profile", choices=["normal", "mixed"], default="normal")
    summary = sub.add_parser("summary", help="Recompute aggregates within one dataset and scope")
    summary.add_argument("dataset_dir", type=Path)
    summary.add_argument("--cell", type=int)
    summary.add_argument("--crnti", type=int)
    summary.add_argument("--ue-uid")
    summary.add_argument("--resource", type=int)
    summary.add_argument("--start", type=datetime.fromisoformat)
    summary.add_argument("--end", type=datetime.fromisoformat)
    quality = sub.add_parser("quality",help="File/block/field quality report")
    quality.add_argument("dataset_dir",type=Path)
    sub.add_parser("sessions",help="List persisted diagnostic sessions")
    sub.add_parser("knowledge-sync",help="Rebuild/incrementally synchronize knowledge source files")
    diagnose = sub.add_parser("diagnose",help="Run the persisted LangGraph diagnostic workflow")
    diagnose.add_argument("dataset_dir",type=Path)
    diagnose.add_argument("question")
    diagnose.add_argument("--session-id")
    diagnose.add_argument("--feature")
    diagnose.add_argument("--cell",type=int)
    diagnose.add_argument("--crnti",type=int)
    diagnose.add_argument("--resource",type=int)
    diagnose.add_argument("--start",type=datetime.fromisoformat)
    diagnose.add_argument("--end",type=datetime.fromisoformat)
    args = parser.parse_args()
    if args.command == "quality":
        from .database import quality_report
        print(json.dumps(quality_report(args.dataset_dir),ensure_ascii=False,indent=2,default=str))
    elif args.command in {"sessions","knowledge-sync","diagnose"}:
        from .agent import DiagnosticEngine
        engine = DiagnosticEngine()
        if args.command=="sessions":
            result = engine.store.list()
        elif args.command=="knowledge-sync":
            result = engine.index.sync()
        else:
            sid = args.session_id or engine.store.create(args.dataset_dir,args.question[:80])
            if Path(engine.store.get(sid)["dataset_dir"]).resolve()!=args.dataset_dir.resolve():
                raise ValueError("Session belongs to a different dataset directory")
            result = engine.run(sid,args.question,Scope(logic_cell_id=args.cell,crnti=args.crnti,resource_id=args.resource,start=args.start,end=args.end),feature_path=args.feature)
            result = {key:result[key] for key in ["session_id","action_id","answer","notices","retrieval_mode","steps"]}
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
    elif args.command == "generate":
        result = generate(args.template,args.output,args.seed,args.blocks,args.files,args.profile)
        print(json.dumps({"path":str(args.output),"records":len(result["truth"]),"seed":result["seed"],"profile":result["profile"]}))
    elif args.command == "summary":
        result = summarize(args.dataset_dir, Scope(args.cell,args.ue_uid,args.crnti,args.resource,args.start,args.end))
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
    elif args.command == "load":
        contract = json.loads(args.contract.read_text(encoding="utf-8"))
        path, meta = build_dataset(args.input_dir, args.storage, contract)
        print(json.dumps({"path": str(path), **meta}, ensure_ascii=False, indent=2))
        if meta["status"] == "FAILED":
            raise SystemExit(1)
    else:
        with duckdb.connect(str(args.dataset_dir / "analysis.duckdb"), read_only=True,
                            config={"enable_external_access": "false"}) as con:
            meta = json.loads(con.execute("SELECT metadata FROM _etl_meta").fetchone()[0])
            if meta["status"] not in ("READY", "PARTIAL_READY"):
                raise ValueError("Dataset is not ready")
            print(json.dumps(meta, ensure_ascii=False, indent=2))
            print(con.execute("SELECT flow_status,data_quality_tag,count(*) AS records FROM l3_cleaned_srs GROUP BY ALL").fetchall())


if __name__ == "__main__":
    main()
