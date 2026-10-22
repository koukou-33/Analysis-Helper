"""Parameterized descriptive queries over a single persisted dataset."""
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import duckdb
from .database import open_dataset


@dataclass(frozen=True)
class Scope:
    logic_cell_id: int | None = None
    ue_uid: str | None = None
    crnti: int | None = None
    resource_id: int | None = None
    start: datetime | None = None
    end: datetime | None = None
    phy_cell_inst: int | None = None

    def where(self):
        if any(value is not None and value.tzinfo is not None for value in (self.start, self.end)):
            raise ValueError("Use local timestamps without a timezone, matching the source CSV")
        if self.start and self.end and self.start >= self.end:
            raise ValueError("Time range must satisfy start < end")
        conditions, parameters = [], []
        for column, value, operator in [
            ("logicCellId", self.logic_cell_id, "="), ("ue_uid", self.ue_uid, "="),
            ("crnti", self.crnti, "="), ("resourceId", self.resource_id, "="),
            ("phyCellInst", self.phy_cell_inst, "="),
            ("event_ts", self.start, ">="), ("event_ts", self.end, "<")]:
            if value is not None:
                conditions.append(f"{column} {operator} ?")
                parameters.append(value)
        return " AND ".join(conditions) or "true", parameters


def summarize(dataset_dir, scope=None, dataset_id=None):
    scope = scope or Scope()
    where, parameters = scope.where()
    with open_dataset(dataset_dir) as (con, meta):
        if meta["status"] not in {"READY", "PARTIAL_READY"} or meta["rule_version"] not in {"m1.2","m1.3"}:
            raise ValueError("Dataset is not ready or has an unsupported schema version")
        if dataset_id is not None and dataset_id != meta["dataset_id"]:
            raise ValueError("Dataset identity mismatch")
        # A temporary scoped relation avoids interpolating values and never changes persisted tables.
        con.execute(f"CREATE TEMP TABLE scoped AS SELECT * FROM l4_ue_resource_ts WHERE {where}", parameters)
        baseline = con.execute("SELECT count(*),count(ue_uid),count(usrRsrp),count(srsUeNiDb),count(*) FILTER(WHERE event_ts IS NULL) FROM scoped").fetchone()
        excluded_time = con.execute("SELECT count(*) FROM l4_ue_resource_ts WHERE event_ts IS NULL").fetchone()[0] if scope.start or scope.end else 0
        def records(sql):
            cursor = con.execute(sql)
            names = [d[0] for d in cursor.description]
            rows = cursor.fetchmany(100001)
            if len(rows)>100000:
                raise ValueError("More than 100,000 summary groups; narrow the analysis scope")
            return [dict(zip(names, row)) for row in rows]
        streams = records("""
            SELECT r.logicCellId,r.ue_uid,r.resourceId,s.streamId,count(*) AS sample_cnt,
              count(s.instBfSinrGain) AS gain_valid_cnt,avg(s.instBfSinrGain) AS gain_avg,
              sum(s.gain_missing::INT) AS gain_missing_cnt,sum(s.gain_parse_error::INT) AS gain_parse_error_cnt,
              avg(r.is_suspicious::INT) AS suspicious_ratio,
              min(r.event_ts) FILTER(WHERE s.instBfSinrGain IS NOT NULL) AS gain_start_time,
              max(r.event_ts) FILTER(WHERE s.instBfSinrGain IS NOT NULL) AS gain_end_time
            FROM scoped r JOIN l4_ue_stream_ts s USING(record_id) WHERE r.ue_uid IS NOT NULL
            GROUP BY r.logicCellId,r.ue_uid,r.resourceId,s.streamId ORDER BY 1,2,3,4
        """)
        groups = records("""
            WITH timed AS (
              SELECT *, min(event_ts) FILTER(WHERE usrRsrp IS NOT NULL) OVER w AS rsrp_start,
                        max(event_ts) FILTER(WHERE usrRsrp IS NOT NULL) OVER w AS rsrp_end
              FROM scoped WHERE ue_uid IS NOT NULL
              WINDOW w AS (PARTITION BY logicCellId,ue_uid,resourceId)
            )
            SELECT logicCellId,ue_uid,resourceId,count(*) AS sample_cnt,
              count(usrRsrp) AS rsrp_valid_cnt,avg(usrRsrp) AS rsrp_avg,stddev_samp(usrRsrp) AS rsrp_std,
              count(srsUeNiDb) AS ni_valid_cnt,avg(srsUeNiDb) AS ni_avg,quantile_cont(srsUeNiDb,0.9) AS ni_p90,
              sum(is_suspicious::INT) AS suspicious_cnt,avg(is_suspicious::INT) AS suspicious_ratio,
              sum(has_missing_measurement::INT) AS missing_cnt,avg(has_missing_measurement::INT) AS missing_ratio,
              sum(has_parse_error::INT) AS parse_error_cnt,min(event_ts) AS start_time,max(event_ts) AS end_time,
              avg(usrRsrp) FILTER(WHERE event_ts>=rsrp_start+(rsrp_end-rsrp_start)/2)
                -avg(usrRsrp) FILTER(WHERE event_ts<rsrp_start+(rsrp_end-rsrp_start)/2) AS rsrp_trend_delta
            FROM timed GROUP BY logicCellId,ue_uid,resourceId ORDER BY 1,2,3
        """)
        totals = dict(con.execute("SELECT ue_uid,count(DISTINCT record_id) FROM scoped WHERE ue_uid IS NOT NULL GROUP BY ue_uid").fetchall())
        cell_tf = records("""SELECT logicCellId,slot_in_frame,freqRbStart,freqRbNum,
            count(*) AS sample_cnt,count(DISTINCT ue_uid) AS session_cnt,
            count(usrRsrp) AS rsrp_valid_cnt,avg(usrRsrp) AS rsrp_avg,
            count(srsUeNiDb) AS ni_valid_cnt,avg(srsUeNiDb) AS ni_avg,
            quantile_cont(srsUeNiDb,0.9) AS ni_p90,max(srsUeNiDb) AS ni_max,
            sum(is_suspicious::INT) AS suspicious_cnt,avg(is_suspicious::INT) AS suspicious_ratio,
            sum(has_missing_measurement::INT) AS missing_cnt,avg(has_missing_measurement::INT) AS missing_ratio,
            sum(has_parse_error::INT) AS parse_error_cnt,min(event_ts) AS start_time,max(event_ts) AS end_time
            FROM scoped GROUP BY logicCellId,slot_in_frame,freqRbStart,freqRbNum ORDER BY 1,2,3,4""")
        by_group = {}
        for stream in streams:
            by_group.setdefault((stream["logicCellId"], stream["ue_uid"], stream["resourceId"]), []).append(stream)
        for group in groups:
            members = by_group[(group["logicCellId"], group["ue_uid"], group["resourceId"])]
            valid = [s for s in members if s["gain_valid_cnt"] > 0]
            group.update(stream_gain_means=[s["gain_avg"] for s in members],
                         stream_gain_valid_counts=[s["gain_valid_cnt"] for s in members],
                         available_streams=[s["streamId"] for s in valid],
                         max_stream_gain_diff=max(s["gain_avg"] for s in valid)-min(s["gain_avg"] for s in valid) if len(valid)>=2 else None,
                         ue_total_records=totals[group["ue_uid"]])
        return {"dataset_id": meta["dataset_id"], "dataset_fingerprint": meta["dataset_fingerprint"],
                "scope": scope.__dict__, "quality_policy": "INCLUDE_ALL_NUMERIC", "gain_unit": "dB",
                "rsrp_ni_unit": "dBm", "mean_policy": "arithmetic mean of finite input values",
                "flow_valid_count": baseline[0], "session_assigned_count": baseline[1],
                "rsrp_valid_count": baseline[2], "ni_valid_count": baseline[3],
                "unlocated_time_count_in_scope": baseline[4], "dataset_unlocated_time_excluded": excluded_time,
                "groups": groups, "streams": streams,"cell_tf":cell_tf}
