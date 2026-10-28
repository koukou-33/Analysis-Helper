"""Dataset validation and resource-bounded read connections."""
from .performance import measured
import json
import hashlib
from contextlib import contextmanager
from pathlib import Path
from threading import Timer

import duckdb


@contextmanager
def open_dataset(directory, seconds=30):
    directory = Path(directory)
    # A short stable path avoids Windows path-length failures when bounded-memory queries spill.
    # All connections to the same dataset must share DuckDB configuration.
    spill_root=Path(__file__).resolve().parent.parent/'workspace_storage'/'query_tmp'
    spill=spill_root/hashlib.sha256(str(directory.resolve()).casefold().encode()).hexdigest()[:24]
    spill.mkdir(parents=True,exist_ok=True)
    con = duckdb.connect(str(directory / "analysis.duckdb"), read_only=True,
                         config={"enable_external_access": "false", "memory_limit": "512MB", "threads": "2", "temp_directory":str(spill)})
    timer = Timer(seconds, con.interrupt)
    timer.daemon = True
    try:
        if con.execute("SELECT count(*) FROM _etl_meta").fetchone()[0]!=1:
            raise ValueError("Dataset completion metadata must have exactly one row")
        meta = json.loads(con.execute("SELECT metadata FROM _etl_meta").fetchone()[0])
        disk = json.loads((directory / "input_manifest.json").read_text(encoding="utf-8"))
        if meta != disk or meta["status"] not in {"READY", "PARTIAL_READY"} or meta["rule_version"] not in {"m1.2","m1.3","m1.4","m1.5"}:
            raise ValueError("Dataset metadata is incomplete, inconsistent, or unsupported")
        from .provenance import active_cohort
        cohort=active_cohort.get()
        if cohort is not None:
            con.register('selected_cohort',cohort)
        timer.start()
        yield con, meta
    finally:
        timer.cancel()
        con.close()


@measured("quality_report")
def quality_report(directory):
    with open_dataset(directory) as (con, meta):
        def rows(sql):
            cur = con.execute(sql)
            names = [c[0] for c in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]
        extra_errors = rows("SELECT field_name,parse_state,count(*) AS records FROM l2_extra_numeric WHERE parse_state<>'VALID' GROUP BY ALL") if meta["rule_version"] in {"m1.3","m1.4","m1.5"} else []
        return {"dataset_id": meta["dataset_id"], "rule_version":meta['rule_version'], "files": meta["files"],"supplemental_field_errors":extra_errors,
                "baseline": rows("""SELECT count(*) AS loaded_count,
                  count(*) FILTER(WHERE NOT csv_structure_complete) AS structure_error_count,
                  count(*) FILTER(WHERE has_parse_error) AS parse_error_count,
                  count(*) FILTER(WHERE uwTtiCnt1 IS NULL) AS invalid_block_key_count
                  FROM l2_standardized""")[0],
                "flow": rows("SELECT flow_status,count(*) AS records FROM l3_cleaned_srs GROUP BY ALL"),
                "blocks": rows("""SELECT source_file,block_seq,block_size,has_missing_crnti,last_good_idx,
                  count(*) FILTER(WHERE flow_status='VALID_FLOW') AS kept_count
                  FROM l3_cleaned_srs GROUP BY ALL ORDER BY source_file,block_seq"""),
                "field_errors": rows("""SELECT json_extract_string(j.value,'$') AS reason,count(*) AS records
                  FROM l2_standardized, json_each(error_reasons) j GROUP BY ALL ORDER BY reason"""),
                "coverage": rows("SELECT min(event_ts) AS start_time,max(event_ts) AS end_time FROM l4_ue_resource_ts")[0]}
