"""Snapshot CSV input and publish an isolated, immutable DuckDB dataset."""
import csv
import hashlib
import json
import math
import re
import shutil
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

import duckdb
import pyarrow as pa
from .schema import bindings, column_catalog

RULE_VERSION = "m1.3"
CAPTURE_NAME = re.compile(r"NR_SRS(?:\(\d+\))?_\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.csv", re.IGNORECASE)
LEGACY_CAPTURE_NAME = re.compile(r"NR_SRS\..+\.csv", re.IGNORECASE)

def is_capture_name(name):
    return bool(CAPTURE_NAME.fullmatch(name) or LEGACY_CAPTURE_NAME.fullmatch(name))


GAIN_FIELDS = tuple(f"instBfSinrGain[{i}]" for i in range(4))
NUMBERS = ("uwTtiCnt1", "logicCellId", "crnti", "phyCellInst", "resourceId",
           "frmNum", "subFrmNum", "slot", "freqRbStart", "freqRbNum", "usrRsrp", "srsUeNiDb") + GAIN_FIELDS
INTEGERS = set(NUMBERS) - {"usrRsrp", "srsUeNiDb", *GAIN_FIELDS}


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def insert_batch(con, table, batch):
    """Bounded Arrow transfer instead of one SQL execution per wide record."""
    if not batch:
        return
    names = [d[0] for d in con.execute(f"SELECT * FROM {table} LIMIT 0").description]
    arrow = pa.Table.from_arrays([pa.array(column) for column in zip(*batch)], names=names)
    con.register("incoming_batch", arrow)
    try:
        con.execute(f"INSERT INTO {table} SELECT * FROM incoming_batch")
    finally:
        con.unregister("incoming_batch")


def normalize(row, header, contract, source, file_hash, row_id, dataset_id, positions=None):
    positions = positions if positions is not None else bindings(header, contract, NUMBERS)
    raw = {name: row[idx] if idx < len(row) else None for name, idx in positions.items()}
    errors = []
    missing = lambda v: v is None or v.strip() in contract["null_values"]
    values = []
    for name in NUMBERS:
        value = raw.get(name)
        if missing(value):
            parsed = None
            errors.append(f"{name}:MISSING_VALUE")
        else:
            try:
                parsed = int(value) if name in INTEGERS else float(value)
                if not math.isfinite(parsed):
                    errors.append(f"{name}:NON_FINITE")
                    parsed = None
                elif name in INTEGERS and not -(2**63) <= parsed < 2**63:
                    errors.append(f"{name}:PARSE_ERROR")
                    parsed = None
            except (ValueError, OverflowError):
                parsed = None
                errors.append(f"{name}:PARSE_ERROR")
        values.append(parsed)
    if len(row) != len(header):
        errors.append("CSV_STRUCTURE_ERROR")
    try:
        event_ts = datetime.strptime(
            f"{raw.get('uwData', '')} {raw.get('uwTime', '')}",
            contract["date_format"] + " " + contract["time_format"])
    except ValueError:
        event_ts = None
        errors.append("INVALID_DATE")
    if values[0] is None:
        errors.append("INVALID_BLOCK_KEY")
    parse_error = any("PARSE_ERROR" in e or "NON_FINITE" in e or e == "INVALID_DATE" for e in errors)
    return (f"{dataset_id}:{source}:{row_id}", source, file_hash, row_id,
            json.dumps(row, ensure_ascii=False), json.dumps(errors), len(row) == len(header),
            not missing(raw.get("crnti")), parse_error,
            missing(raw.get("usrRsrp")), missing(raw.get("srsUeNiDb")),
            raw.get("uwData"), raw.get("uwTime"), event_ts, *values)


def build_dataset(input_dir, storage, contract):
    contract = dict(contract)
    if "field_mapping_file" in contract:
        contract["field_mapping"] = json.loads(Path(contract["field_mapping_file"]).read_text(encoding="utf-8"))
    mapping = contract.get("field_mapping", {})
    if mapping.get("stream_count", 4) != 4 or mapping.get("stream_gain_family", "instBfSinrGain") != "instBfSinrGain" or mapping.get("gain_scale", 1.0) != 1.0:
        raise ValueError("Only four instBfSinrGain streams with scale 1 are currently supported")
    if contract["flow_presence_column"] != "crnti":
        raise ValueError("Flow presence must use raw crnti")
    root, storage = Path(input_dir).resolve(), Path(storage).resolve()
    paths = [root] if root.is_file() and root.suffix.lower() == ".csv" else sorted(p for p in root.rglob("*") if p.is_file() and is_capture_name(p.name))
    if root.is_file():
        root = root.parent
    if not paths:
        raise ValueError("No matching NR_SRS capture CSV files found")
    dataset_id = str(uuid.uuid4())
    build = storage / (".building-" + dataset_id)
    build.mkdir(parents=True)
    (build / "snapshot").mkdir()
    manifest, seen = [], {}
    con = None
    try:
        for path in paths:
            source = path.relative_to(root).as_posix()
            entry = {"source_file": source}
            manifest.append(entry)
            try:
                snapshot = build / "snapshot" / source
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                for attempt in range(3):
                    before = digest(path)
                    shutil.copyfile(path, snapshot)
                    if before == digest(snapshot) == digest(path):
                        break
                else:
                    raise ValueError("Source changed during snapshot")
                entry.update(sha256=before, size=snapshot.stat().st_size, status="SNAPSHOT")
                key = (before, snapshot.stat().st_size)
                if key in seen:
                    # Cryptographic equality is supplemented by a bounded byte comparison.
                    with snapshot.open("rb") as a, seen[key][1].open("rb") as b:
                        equal = True
                        while chunk := a.read(1024 * 1024):
                            if chunk != b.read(len(chunk)):
                                equal = False
                                break
                    if equal:
                        entry.update(status="DUPLICATE", duplicate_of=seen[key][0])
                        continue
                seen[key] = (source, snapshot)
            except (OSError, ValueError) as exc:
                entry.update(status="FAILED", error=str(exc))
        fingerprint = hashlib.sha256(json.dumps(
            {"inputs": manifest, "contract": contract, "rules": RULE_VERSION},
            sort_keys=True).encode()).hexdigest()
        con = duckdb.connect(str(build / "analysis.duckdb"))
        con.execute("CREATE TABLE field_catalog (source_file VARCHAR,column_ordinal INTEGER,raw_name VARCHAR,"
                    "family VARCHAR,array_index INTEGER,array_extent INTEGER,domain VARCHAR,"
                    "present_count BIGINT,missing_count BIGINT,availability VARCHAR)")
        con.execute("CREATE TABLE l2_extra_numeric (record_id VARCHAR,field_name VARCHAR,value DOUBLE,parse_state VARCHAR)")
        numeric_schema = ", ".join(f'"{n}" {"BIGINT" if n in INTEGERS else "DOUBLE"}' for n in NUMBERS)
        con.execute("CREATE TABLE l2_standardized (record_id VARCHAR PRIMARY KEY, source_file VARCHAR, "
                    "source_file_hash VARCHAR, file_row_id BIGINT, raw_values JSON, error_reasons JSON, "
                    "csv_structure_complete BOOLEAN, crnti_present BOOLEAN, has_parse_error BOOLEAN, "
                    "rsrp_missing BOOLEAN, ni_missing BOOLEAN, uwData VARCHAR, uwTime VARCHAR, event_ts TIMESTAMP, " + numeric_schema + ")")
        for entry in manifest:
            if entry["status"] != "SNAPSHOT":
                continue
            row_id = 0
            con.execute("BEGIN")
            try:
                with (build / "snapshot" / entry["source_file"]).open(
                        encoding=contract["encoding"], newline="") as stream:
                    reader = csv.reader(stream, delimiter=contract["delimiter"], strict=True)
                    header = next(reader)
                    positions = bindings(header, contract, NUMBERS)
                    folded = Counter(name.casefold() for name in header)
                    reserved = {name.casefold() for name in NUMBERS} | {"uwdata","uwdate","uwtime","record_id","source_file","event_ts","ue_uid","streamid"}
                    mapped_positions = set(positions.values())
                    extras = [(i,name) for i,name in enumerate(header) if i not in mapped_positions and folded[name.casefold()]==1
                              and name.casefold() not in reserved and "[" not in name and name and len(name)<=100]
                    catalog = column_catalog(header, mapping)
                    present = [0] * len(header)
                    entry["columns"] = header
                    batch = []
                    extra_batch = []
                    for row_id, row in enumerate(reader, 1):
                        for idx, value in enumerate(row[:len(header)]):
                            present[idx] += value.strip() not in contract["null_values"]
                        batch.append(normalize(row, header, contract, entry["source_file"],
                                               entry["sha256"], row_id, dataset_id, positions))
                        for idx,name in extras:
                            if idx>=len(row) or row[idx].strip() in contract["null_values"]:
                                continue
                            try:
                                value = float(row[idx])
                                status = "VALID" if math.isfinite(value) else "NON_FINITE"
                            except ValueError:
                                value,status = None,"PARSE_ERROR"
                            extra_batch.append((batch[-1][0],name,value if status=="VALID" else None,status))
                        if len(batch) == 2000:
                            insert_batch(con, "l2_standardized", batch)
                            insert_batch(con, "l2_extra_numeric", extra_batch)
                            batch.clear()
                            extra_batch.clear()
                    if batch:
                        insert_batch(con, "l2_standardized", batch)
                        insert_batch(con, "l2_extra_numeric", extra_batch)
                    insert_batch(con, "field_catalog", [
                        (entry["source_file"], *col, present[col[0]], row_id-present[col[0]],
                         "NO_RECORDS" if row_id == 0 else "ALL_NULL" if present[col[0]] == 0 else
                         "AVAILABLE" if present[col[0]] == row_id else "PARTIAL_MISSING") for col in catalog])
                    entry["unavailable_mapped_fields"] = sorted(set(NUMBERS) - positions.keys())
                con.execute("COMMIT")
                entry.update(status="LOADED", records=row_id)
            except (OSError, UnicodeError, csv.Error, ValueError, StopIteration, duckdb.Error) as exc:
                con.execute("ROLLBACK")
                entry.update(status="FAILED", error=str(exc), record_near=row_id + 1)
        con.execute(PIPELINE_SQL)
        status = "FAILED" if not any(e["status"] == "LOADED" for e in manifest) else (
            "PARTIAL_READY" if any(e["status"] == "FAILED" for e in manifest) else "READY")
        metadata = {"dataset_id": dataset_id, "dataset_fingerprint": fingerprint,
                    "rule_version": RULE_VERSION, "status": status, "files": manifest,
                    "contract": contract, "completed_at": datetime.now().isoformat()}
        manifest_text = json.dumps(metadata, ensure_ascii=False, indent=2)
        con.execute("CREATE TABLE _etl_meta AS SELECT ?::JSON AS metadata", [manifest_text])
        con.close()
        con = None
        (build / "input_manifest.json").write_text(manifest_text, encoding="utf-8")
        # Unique publication path prevents overwriting historical evidence or concurrent builds.
        final = storage / f"{fingerprint}-{dataset_id}"
        build.rename(final)
        return final, metadata
    except BaseException:
        if con is not None:
            con.close()
        # Keep unpublished build for diagnosis; never expose it as a READY dataset.
        raise


PIPELINE_SQL = """
CREATE TABLE l3_cleaned_srs AS
WITH prev AS (
 SELECT *, lag(uwTtiCnt1) OVER w AS prev_tti, lag(file_row_id) OVER w AS prev_id
 FROM l2_standardized WHERE uwTtiCnt1 IS NOT NULL
 WINDOW w AS (PARTITION BY source_file ORDER BY file_row_id)
), blocks AS (
 SELECT *, sum(CASE WHEN prev_id IS NULL OR file_row_id <> prev_id+1 OR uwTtiCnt1 <> prev_tti
 THEN 1 ELSE 0 END) OVER (PARTITION BY source_file ORDER BY file_row_id) AS block_seq FROM prev
), numbered AS (
 SELECT *, row_number() OVER (PARTITION BY source_file, block_seq ORDER BY file_row_id)-1 AS idx_in_block FROM blocks
), stats AS (
 SELECT *, count(*) OVER w AS block_size, bool_or(NOT crnti_present) OVER w AS has_missing_crnti,
 max(CASE WHEN crnti_present THEN idx_in_block END) OVER w AS last_good_idx
 FROM numbered WINDOW w AS (PARTITION BY source_file, block_seq)
), flow AS (
 SELECT *, CASE WHEN NOT has_missing_crnti OR (idx_in_block < last_good_idx AND crnti_present)
 THEN 'VALID_FLOW' ELSE 'INVALID_FLOW_CTRL' END AS flow_status FROM stats
), flags AS (
 SELECT *, flow_status='VALID_FLOW' AND (coalesce(usrRsrp<=-160 OR usrRsrp>=-60,false)
 OR coalesce(srsUeNiDb<=-130 OR srsUeNiDb>=-80,false)) AS is_suspicious,
 flow_status='VALID_FLOW' AND (rsrp_missing OR ni_missing) AS has_missing_measurement FROM flow
)
SELECT *, CASE WHEN flow_status <> 'VALID_FLOW' THEN 'INVALID_FLOW_CTRL'
 WHEN has_parse_error THEN 'PARSE_ERROR' WHEN is_suspicious THEN 'SUSPICIOUS'
 WHEN has_missing_measurement THEN 'MISSING_DATA' ELSE 'VALID_NORMAL' END AS data_quality_tag
FROM flags;
CREATE TABLE quarantined_rows AS SELECT * FROM l2_standardized WHERE uwTtiCnt1 IS NULL;
CREATE TABLE assigned_sessions AS
WITH prev AS (
 SELECT record_id, logicCellId, crnti, event_ts,
 lag(event_ts) OVER (PARTITION BY logicCellId,crnti ORDER BY event_ts,frmNum,subFrmNum*2+slot,source_file,file_row_id) AS previous_ts
 FROM l3_cleaned_srs WHERE flow_status='VALID_FLOW' AND logicCellId IS NOT NULL
 AND crnti IS NOT NULL AND event_ts IS NOT NULL
), segments AS (
 SELECT *, sum(CASE WHEN previous_ts IS NULL OR event_ts-previous_ts > INTERVAL 180 SECOND
 THEN 1 ELSE 0 END) OVER (PARTITION BY logicCellId,crnti ORDER BY event_ts) AS segment FROM prev
)
SELECT record_id, concat(logicCellId,'_',crnti,'_',
 strftime(min(event_ts) OVER (PARTITION BY logicCellId,crnti,segment),'%Y-%m-%dT%H:%M:%S.%f')) AS ue_uid FROM segments;
CREATE TABLE l4_ue_resource_ts AS
SELECT l.*, s.ue_uid, CASE WHEN s.ue_uid IS NULL THEN 'IDENTITY_OR_TIME_UNAVAILABLE' END AS session_unassigned_reason,
 epoch(event_ts-(SELECT min(event_ts) FROM l2_standardized)) AS time_sec,
 CASE WHEN subFrmNum BETWEEN 0 AND 9 AND slot BETWEEN 0 AND 1 THEN subFrmNum*2+slot END AS slot_in_frame,
 CASE WHEN freqRbNum>0 THEN freqRbStart+freqRbNum-1 END AS freqRbEnd
FROM l3_cleaned_srs l LEFT JOIN assigned_sessions s USING(record_id) WHERE flow_status='VALID_FLOW';
CREATE TABLE l4_ue_summary AS SELECT logicCellId,ue_uid,resourceId,
 count(*) AS sample_cnt, count(usrRsrp) AS rsrp_valid_cnt, count(srsUeNiDb) AS ni_valid_cnt,
 avg(usrRsrp) AS rsrp_avg, stddev_samp(usrRsrp) AS rsrp_std,
 avg(srsUeNiDb) AS ni_avg, quantile_cont(srsUeNiDb,0.9) AS ni_p90,
 count(*) FILTER(WHERE is_suspicious) AS suspicious_cnt, avg(is_suspicious::INT) AS suspicious_ratio,
 count(*) FILTER(WHERE has_missing_measurement) AS missing_cnt, avg(has_missing_measurement::INT) AS missing_ratio,
 count(*) FILTER(WHERE has_parse_error) AS parse_error_cnt, min(event_ts) AS start_time,max(event_ts) AS end_time
FROM l4_ue_resource_ts WHERE ue_uid IS NOT NULL GROUP BY logicCellId,ue_uid,resourceId;
CREATE TABLE l4_cell_tf_summary AS SELECT logicCellId,slot_in_frame,freqRbStart,freqRbNum,
 count(*) AS sample_cnt, count(DISTINCT ue_uid) AS session_cnt,
 count(usrRsrp) AS rsrp_valid_cnt,count(srsUeNiDb) AS ni_valid_cnt,
 avg(usrRsrp) AS rsrp_avg,avg(srsUeNiDb) AS ni_avg,quantile_cont(srsUeNiDb,0.9) AS ni_p90,max(srsUeNiDb) AS ni_max,
 count(*) FILTER(WHERE is_suspicious) AS suspicious_cnt,avg(is_suspicious::INT) AS suspicious_ratio,
 count(*) FILTER(WHERE has_missing_measurement) AS missing_cnt,avg(has_missing_measurement::INT) AS missing_ratio,
 count(*) FILTER(WHERE has_parse_error) AS parse_error_cnt,min(event_ts) AS start_time,max(event_ts) AS end_time
FROM l4_ue_resource_ts GROUP BY logicCellId,slot_in_frame,freqRbStart,freqRbNum;
CREATE TABLE l4_ue_stream_ts AS
SELECT r.*, s.streamId,
 list_extract(["instBfSinrGain[0]","instBfSinrGain[1]","instBfSinrGain[2]","instBfSinrGain[3]"],s.streamId+1) AS instBfSinrGain,
 json_contains(error_reasons,to_json(concat('instBfSinrGain[',s.streamId,']:MISSING_VALUE'))) AS gain_missing,
 json_contains(error_reasons,to_json(concat('instBfSinrGain[',s.streamId,']:PARSE_ERROR')))
 OR json_contains(error_reasons,to_json(concat('instBfSinrGain[',s.streamId,']:NON_FINITE'))) AS gain_parse_error
FROM l4_ue_resource_ts r CROSS JOIN range(4) s(streamId);
CREATE UNIQUE INDEX stream_record_key ON l4_ue_stream_ts(record_id,streamId);
CREATE TABLE l4_ue_stream_summary AS
SELECT logicCellId,ue_uid,resourceId,streamId,count(*) AS sample_cnt,
 count(instBfSinrGain) AS gain_valid_cnt,avg(instBfSinrGain) AS gain_avg,
 count(*) FILTER(WHERE gain_missing) AS gain_missing_cnt,
 count(*) FILTER(WHERE gain_parse_error) AS gain_parse_error_cnt,
 count(*) FILTER(WHERE is_suspicious) AS suspicious_cnt,avg(is_suspicious::INT) AS suspicious_ratio,
 min(event_ts) FILTER(WHERE instBfSinrGain IS NOT NULL) AS gain_start_time,
 max(event_ts) FILTER(WHERE instBfSinrGain IS NOT NULL) AS gain_end_time
FROM l4_ue_stream_ts WHERE ue_uid IS NOT NULL GROUP BY logicCellId,ue_uid,resourceId,streamId;
CREATE OR REPLACE TABLE l4_ue_summary AS
WITH gains AS (
 SELECT logicCellId,ue_uid,resourceId,
 list(gain_avg ORDER BY streamId) AS stream_gain_means,
 list(gain_valid_cnt ORDER BY streamId) AS stream_gain_valid_counts,
 list(streamId ORDER BY streamId) FILTER(WHERE gain_valid_cnt>0) AS available_streams,
 count(*) FILTER(WHERE gain_valid_cnt>0) AS available_stream_count,
 CASE WHEN count(gain_avg)>=2 THEN max(gain_avg)-min(gain_avg) END AS max_stream_gain_diff
 FROM l4_ue_stream_summary GROUP BY logicCellId,ue_uid,resourceId
), midpoints AS (
 SELECT logicCellId,ue_uid,resourceId,min(event_ts)+(max(event_ts)-min(event_ts))/2 AS midpoint
 FROM l4_ue_resource_ts WHERE ue_uid IS NOT NULL AND usrRsrp IS NOT NULL AND event_ts IS NOT NULL
 GROUP BY logicCellId,ue_uid,resourceId
), trends AS (
 SELECT r.logicCellId,r.ue_uid,r.resourceId,
 avg(usrRsrp) FILTER(WHERE event_ts>=midpoint)-avg(usrRsrp) FILTER(WHERE event_ts<midpoint) AS rsrp_trend_delta
 FROM l4_ue_resource_ts r JOIN midpoints m ON r.ue_uid=m.ue_uid AND r.logicCellId=m.logicCellId
 AND r.resourceId IS NOT DISTINCT FROM m.resourceId GROUP BY r.logicCellId,r.ue_uid,r.resourceId
), totals AS (
 SELECT ue_uid,count(DISTINCT record_id) AS ue_total_records FROM l4_ue_resource_ts
 WHERE ue_uid IS NOT NULL GROUP BY ue_uid
)
SELECT u.*,g.stream_gain_means,g.stream_gain_valid_counts,g.available_streams,g.available_stream_count,
 g.max_stream_gain_diff,t.rsrp_trend_delta,n.ue_total_records
FROM l4_ue_summary u JOIN gains g ON u.ue_uid=g.ue_uid AND u.logicCellId=g.logicCellId
 AND u.resourceId IS NOT DISTINCT FROM g.resourceId
LEFT JOIN trends t ON u.ue_uid=t.ue_uid AND u.logicCellId=t.logicCellId
 AND u.resourceId IS NOT DISTINCT FROM t.resourceId JOIN totals n ON u.ue_uid=n.ue_uid;
"""
