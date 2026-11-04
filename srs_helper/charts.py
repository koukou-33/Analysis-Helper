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

def metric_time_series(directory,scope,folder,field):
    """Full raw observations, with missing/conflicting values and quality barriers."""
    from .plans import prepare_scope, scope_columns, identifier
    with open_dataset(directory) as (con,meta):
        if field not in scope_columns(con)['resource']:
            raise ValueError('字段不存在：'+field)
        prepare_scope(con,scope,[field],include_stream=False)
        q=identifier(field,[c[0] for c in con.execute('DESCRIBE resource').fetchall()])
        where,params=scope.where()
        con.execute('''CREATE TEMP TABLE trend_points AS
            WITH points AS (
              SELECT logicCellId,ue_uid,event_ts,count(*) AS sample_cnt,list(record_id ORDER BY record_id) AS record_ids,
                CASE WHEN bool_and(csv_structure_complete) AND count(DISTINCT '''+q+''')=1 AND count('''+q+''')=count(*) THEN min('''+q+''') END AS value
              FROM resource WHERE event_ts IS NOT NULL AND ue_uid IS NOT NULL GROUP BY ALL
            ), barriers AS (
              SELECT l.logicCellId,s.ue_uid,l.event_ts FROM l3_cleaned_srs l
              LEFT JOIN assigned_sessions s USING(record_id) WHERE ('''+where+''') AND NOT business_eligible
            ) SELECT p.*, EXISTS(SELECT 1 FROM barriers b WHERE b.logicCellId=p.logicCellId AND b.ue_uid=p.ue_uid
                AND b.event_ts=p.event_ts) AS barrier FROM points p ORDER BY logicCellId,ue_uid,event_ts''',params)
        # Excluded observations between eligible points must also break a segment.
        con.execute('''CREATE TEMP TABLE trend AS SELECT *,sum(CASE WHEN value IS NULL OR barrier OR EXISTS(
            SELECT 1 FROM l3_cleaned_srs x JOIN assigned_sessions a USING(record_id)
            WHERE NOT x.business_eligible AND a.ue_uid=t.ue_uid AND x.logicCellId=t.logicCellId
            AND x.event_ts>t.previous_ts AND x.event_ts<t.event_ts) THEN 1 ELSE 0 END)
            OVER(PARTITION BY logicCellId,ue_uid ORDER BY event_ts) AS segment
            FROM (SELECT *,lag(event_ts) OVER(PARTITION BY logicCellId,ue_uid ORDER BY event_ts) AS previous_ts FROM trend_points) t''')
        artifact=export_relation(con,'trend',Path(folder),default_plan(meta,'时序 '+field),scope,'METRIC_TIME_SERIES')
        stats=con.execute('SELECT count(*),count(value),min(value),max(value),min(event_ts),max(event_ts) FROM trend').fetchone()
        groups=con.execute('SELECT logicCellId,ue_uid,count(value) AS valid_points,first(value ORDER BY event_ts) FILTER(WHERE value IS NOT NULL) AS first_value,last(value ORDER BY event_ts) FILTER(WHERE value IS NOT NULL) AS last_value,min(value) AS minimum,max(value) AS maximum FROM trend GROUP BY logicCellId,ue_uid').fetchall()
        artifact.update(field=field,group_statistics=[dict(zip(['logicCellId','ue_uid','valid_points','first_value','last_value','minimum','maximum'],row)) for row in groups],statistics=dict(zip(['points','valid_points','minimum','maximum','start','end'],stats)),
                        unit='原始值（单位/缩放待确认）',group_by=['logicCellId','ue_uid'])
        return artifact
