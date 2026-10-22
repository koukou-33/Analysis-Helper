"""Immutable second-bucket time-series evidence, independent of chart preview."""
from pathlib import Path

from .database import open_dataset
from .plans import default_plan, export_relation


def time_series_artifact(directory,scope,output_dir):
    Path(output_dir).mkdir(parents=True,exist_ok=True)
    with open_dataset(directory) as (con,meta):
        where,params = scope.where()
        sql = f"""SELECT logicCellId,ue_uid,resourceId,date_trunc('second',event_ts) AS time_bucket,
          count(*) AS sample_cnt,count(usrRsrp) AS rsrp_valid_cnt,avg(usrRsrp) AS rsrp_avg,
          count(srsUeNiDb) AS ni_valid_cnt,avg(srsUeNiDb) AS ni_avg,
          avg(is_suspicious::INT) AS suspicious_ratio,sum(has_missing_measurement::INT) AS missing_cnt
          FROM l4_ue_resource_ts WHERE ({where}) AND event_ts IS NOT NULL AND ue_uid IS NOT NULL
          GROUP BY logicCellId,ue_uid,resourceId,time_bucket ORDER BY time_bucket,logicCellId,ue_uid,resourceId"""
        con.execute("CREATE TEMP TABLE time_series AS "+sql,params)
        artifact = export_relation(con,"time_series",Path(output_dir),default_plan(meta,"time series"),scope,"TIME_SERIES")
        artifact.update(sql=sql,parameters=params,bucket_seconds=1,units={"rsrp_avg":"dBm","ni_avg":"dBm"})
        return artifact
