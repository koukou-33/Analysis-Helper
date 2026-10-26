"""Question-triggered descriptive statistics; no unconfirmed fault thresholds."""
from pathlib import Path
from .database import open_dataset
from .plans import default_plan, export_relation


def describe_cross_layer(directory,scope,question,fields,output_dir):
    artifacts=[]
    Path(output_dir).mkdir(parents=True,exist_ok=True)
    trend = '趋势' in question or 'trend' in question.lower()
    hopping = '跳频' in question or 'hopping' in question.lower()
    if not trend and not hopping:
        return artifacts
    with open_dataset(directory) as (con,meta):
        where,params=scope.where()
        queries=[]
        if hopping:
            queries.append(('rb_intervals',f"""SELECT logicCellId,ue_uid,resourceId,
                count(DISTINCT (freqRbStart,freqRbNum)) AS rb_interval_count,count(*) AS valid_count
                FROM l4_ue_resource_ts WHERE ({where}) AND ue_uid IS NOT NULL AND freqRbStart IS NOT NULL AND freqRbNum>0
                GROUP BY logicCellId,ue_uid,resourceId""",'范围内不同 RB 起点/数量组合数；不是切换次数，不推断跳频故障'))
        if trend:
            for field in sorted(set(fields)&{'usrRsrp','srsUeNiDb','instBfSinrGain'}):
                table='l4_ue_stream_ts' if field=='instBfSinrGain' else 'l4_ue_resource_ts'
                keys='logicCellId,ue_uid,resourceId'+(',streamId' if field=='instBfSinrGain' else '')
                queries.append(('trend_'+field,f"""WITH buckets AS (
                    SELECT {keys},date_trunc('minute',event_ts) AS minute,avg("{field}") AS mean,count(*) AS valid_count
                    FROM {table} WHERE ({where}) AND ue_uid IS NOT NULL AND event_ts IS NOT NULL AND "{field}" IS NOT NULL
                    GROUP BY {keys},minute)
                    SELECT {keys},count(*) AS bucket_count,sum(valid_count) AS valid_count,
                    min(minute) AS start_minute,max(minute) AS end_minute,
                    CASE WHEN count(*)>=2 THEN arg_max(mean,minute)-arg_min(mean,minute) END AS trend_delta
                    FROM buckets GROUP BY {keys}""",'最后一个有值自然分钟均值减第一个有值自然分钟均值；少于两桶返回空，不自动判异常'))
        for name,sql,purpose in queries:
            con.execute(f'CREATE TEMP TABLE "{name}" AS '+sql,params)
            artifact=export_relation(con,name,Path(output_dir),default_plan(meta,question),scope,'DESCRIPTIVE_CROSS_LAYER')
            artifact.update(sql=sql,parameters=params,purpose=purpose,origin='QUESTION_DESCRIPTIVE')
            artifacts.append(artifact)
    return artifacts
