import argparse
import json
from pathlib import Path

import duckdb

from .pipeline import build_dataset


def main():
    parser = argparse.ArgumentParser(description="SRS deterministic data foundation")
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("load")
    load.add_argument("input_dir", type=Path)
    load.add_argument("--storage", type=Path, default=Path("workspace_storage/datasets"))
    load.add_argument("--contract", type=Path, default=Path("config/input_contract.json"))
    inspect = sub.add_parser("inspect")
    inspect.add_argument("dataset_dir", type=Path)
    args = parser.parse_args()
    if args.command == "load":
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
