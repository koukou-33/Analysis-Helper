"""Explicit opt-in peer baseline, with exact scheduling-position matching."""
from pathlib import Path
from .database import open_dataset
from .plans import QueryPlan, export_relation


def compare_peers(directory, scope, fields, output_dir, *, enabled=False):
    if not enabled:
        return {"status":"NOT_REQUESTED", "artifacts":[]}
    if scope.ue_uid is None and scope.crnti is None:
        return {"status":"TARGET_REQUIRED", "reason":"请先选择目标 UE 或 CRNTI。", "artifacts":[]}
    allowed = {'usrRsrp','srsUeNiDb','instBfSinrGain'}
    fields = sorted(set(fields) & allowed)
    if not fields:
        return {"status":"METRIC_REQUIRED", "reason":"请明确比较 RSRP、NI 或增益。", "artifacts":[]}
    artifacts = []
    matched = False
    Path(output_dir).mkdir(parents=True,exist_ok=True)
    with open_dataset(directory) as (con,meta):
        if meta['rule_version'] not in {'m1.4','m1.5'}:
            return {"status":"RELOAD_REQUIRED", "reason":"请重新加载，以应用实际流数和整行排除规则。", "artifacts":[]}
        where,params = scope.where()
        con.execute(f'CREATE TEMP TABLE target AS SELECT * FROM l4_ue_resource_ts WHERE {where}', params)
        required = ['logicCellId','event_ts','freqRbStart','freqRbNum','slot_in_frame','ueSymPos','crnti']
        missing = con.execute('SELECT count(*) FROM target WHERE '+' OR '.join(f'{c} IS NULL' for c in required)).fetchone()[0]
        # Symbol count is not assumed if a supplied count explicitly contradicts single-symbol semantics.
        invalid_symbols = "NOT EXISTS (SELECT 1 FROM l2_extra_numeric e WHERE e.record_id=r.record_id AND e.field_name='symbolNum' AND (e.value IS NULL OR e.value<>1))"
        for n,field in enumerate(fields):
            table = 'l4_ue_stream_ts' if field=='instBfSinrGain' else 'l4_ue_resource_ts'
            stream = ',streamId' if field=='instBfSinrGain' else ''
            keys = 'logicCellId,minute,freqRbStart,freqRbNum,slot_in_frame,ueSymPos'+stream
            source = f"""SELECT r.*,date_trunc('minute',event_ts) AS minute FROM {table} r
                WHERE business_eligible AND ue_uid IS NOT NULL AND {invalid_symbols}"""
            con.execute(f'CREATE OR REPLACE TEMP TABLE candidate AS {source}')
            con.execute(f"""CREATE OR REPLACE TEMP TABLE peer_result AS
              WITH t AS (SELECT {keys},avg("{field}") AS target_avg,count("{field}") AS target_valid_count
                FROM candidate SEMI JOIN target USING(record_id)
                WHERE {field} IS NOT NULL GROUP BY {keys}),
              users AS (SELECT {keys},ue_uid,avg("{field}") AS peer_avg,count("{field}") AS valid_count
                FROM candidate p WHERE {field} IS NOT NULL
                AND NOT EXISTS(SELECT 1 FROM target t WHERE t.logicCellId=p.logicCellId AND t.crnti=p.crnti)
                GROUP BY {keys},ue_uid),
              baseline AS (SELECT {keys},count(*) AS peer_ue_count,avg(peer_avg) AS peer_avg,
                sum(valid_count) AS peer_sample_count, list(struct_pack(ue_uid:=ue_uid,valid_count:=valid_count,mean:=peer_avg)) AS peer_members FROM users GROUP BY {keys})
              SELECT t.*,b.peer_avg,b.peer_ue_count,b.peer_sample_count,b.peer_members,t.target_avg-b.peer_avg AS comparison_diff
              FROM t LEFT JOIN baseline b USING({keys})""")
            plan = QueryPlan(dataset_id=meta['dataset_id'],dataset_fingerprint=meta['dataset_fingerprint'],
                             question='显式同侪比较',steps=[{'step_id':'s1','operator':'GROUP_AGG','metrics':[{'field':field,'aggregate':'count','output':'n'}]}])
            artifact = export_relation(con,'peer_result',Path(output_dir),plan,scope,'PEER_COMPARISON')
            artifact.update(metric=field,unit='dB' if field=='instBfSinrGain' else 'dBm',origin='EXPLICIT_PEER_COMPARISON')
            artifacts.append(artifact)
            matched = matched or con.execute("SELECT count(*) FROM peer_result WHERE peer_ue_count>0").fetchone()[0]>0
    return {"status":"COMPLETED" if matched else "NO_MATCH", "artifacts":artifacts,
            "missing_position_records":missing, "window":"natural minute, source local event time",
            "aggregation":"先按 UE 求均值，再对 UE 均值等权平均；不等于逐样本加权",
            "limitation":"空间位置差异未受控；匹配不构成因果或根因证明。"}
