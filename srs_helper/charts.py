"""Immutable time-series evidence, independent of chart preview."""
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
    Path(folder).mkdir(parents=True,exist_ok=True)
    from .plans import prepare_scope, scope_columns, identifier
    with open_dataset(directory) as (con,meta):
        if field not in scope_columns(con)['resource']:
            raise ValueError('字段不存在：'+field)
        prepare_scope(con,scope,[field],include_stream=False)
        q=identifier(field,[c[0] for c in con.execute('DESCRIBE resource').fetchall()])
        from .observations import create_observations, VERSION, AXIS_POLICY, LIMITATIONS
        create_observations(con,scope,q)
        artifact=export_relation(con,'trend',Path(folder),default_plan(meta,'时序 '+field),scope,
                                 'METRIC_TIME_SERIES',order_by=['logicCellId','ue_uid','series_id','observation_index'])
        stats=con.execute('SELECT count(*),count(value),min(value),max(value),min(event_ts),max(event_ts),'
                          'coalesce(sum(sample_cnt),0),coalesce(sum(eligible_count),0),'
                          'count(*) FILTER(WHERE conflict),count(*) FILTER(WHERE barrier),'
                          'count(*) FILTER(WHERE changed) FROM trend').fetchone()
        group_columns=['logicCellId','ue_uid','series_id','source_file','phyCellInst','resourceId','freqRbStart','freqRbNum']
        groups=con.execute('SELECT '+','.join(group_columns)+''',count(value) AS valid_points,
            CASE WHEN bool_and(ordering_status IN ('START','TIMESTAMP_ORDER','COUNTER_ORDER','FRAME_ORDER'))
              THEN first(value ORDER BY observation_index) FILTER(WHERE value IS NOT NULL) END AS first_value,
            CASE WHEN bool_and(ordering_status IN ('START','TIMESTAMP_ORDER','COUNTER_ORDER','FRAME_ORDER'))
              THEN last(value ORDER BY observation_index) FILTER(WHERE value IS NOT NULL) END AS last_value,
            min(value) AS minimum,max(value) AS maximum,count(*) AS points,sum(sample_cnt) AS source_rows,
            count(*) FILTER(WHERE changed) AS observed_changes,
            count(*) FILTER(WHERE ordering_status NOT IN ('START','TIMESTAMP_ORDER','COUNTER_ORDER','FRAME_ORDER')) AS order_breaks
            FROM trend GROUP BY ALL ORDER BY logicCellId,ue_uid,series_id''').fetchall()
        from .result_summary import summarize_relation, field_units
        metric_unit=field_units().get(field,'raw')
        artifact.update(field=field,observation_version=VERSION,axis_policy=AXIS_POLICY,
                        group_statistics=[dict(zip(group_columns+['valid_points','first_value','last_value','minimum','maximum','points','source_rows','observed_changes','order_breaks'],row)) for row in groups],
                        statistics=dict(zip(['points','valid_points','minimum','maximum','start','end','source_rows','eligible_rows','conflict_points','barrier_points','observed_changes'],stats)),
                        unit='原始值（单位/缩放待确认）' if metric_unit=='raw' else metric_unit,
                        group_by=group_columns,limitations=LIMITATIONS)
        summarize_relation(con,'trend',artifact,source_units={'value':metric_unit},
                           extra={'field':field,'statistics':artifact['statistics'],
                                  'observation_version':VERSION,'axis_policy':AXIS_POLICY,
                                  'limitations':LIMITATIONS,'group_by':group_columns,
                                  'group_statistics':artifact['group_statistics'][:200],
                                  'group_statistics_truncated':len(groups)>200})
        return artifact
