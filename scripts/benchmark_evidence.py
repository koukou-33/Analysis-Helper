"""Synthetic executed-result benchmark; no production data or model calls."""
import json
import time
from pathlib import Path
import duckdb
from srs_helper.plans import QueryPlan, Step, Expression, export_relation
from srs_helper.queries import Scope
from srs_helper.result_summary import summarize_relation, read_summary


def main():
    root=Path('workspace_storage/evidence_benchmark')
    root.mkdir(parents=True,exist_ok=True)
    with duckdb.connect(config={'memory_limit':'512MB','threads':'2'}) as con:
        con.execute('CREATE TABLE _etl_meta AS SELECT ? metadata',[json.dumps({'contract':{}})])
        con.execute('''CREATE TABLE resource AS SELECT i::VARCHAR AS record_id,
            i%4 AS logicCellId, (i%1000)::VARCHAR AS ue_uid,
            TIMESTAMP '2026-11-09' + i * INTERVAL 1 SECOND AS event_ts,
            i%2 AS enabled, (i%100)::DOUBLE AS taValue,
            i::DOUBLE AS f1,i::DOUBLE AS f2,i::DOUBLE AS f3,i::DOUBLE AS f4
            FROM range(120000) t(i)''')
        step=Step(step_id='s1',operator='FILTER',condition=Expression(op='ge',field='taValue',value=0))
        plan=QueryPlan(dataset_id='synthetic',dataset_fingerprint='synthetic',question='TA',steps=[step])
        con.execute('CREATE TABLE s1 AS SELECT * FROM resource')
        started=time.perf_counter()
        artifact=export_relation(con,'s1',root,plan,Scope(),'FILTER')
        exported=time.perf_counter()
        summarize_relation(con,'s1',artifact,step,{'taValue':'raw'})
        ended=time.perf_counter()
        facts={f['fact_id']:f['value'] for f in read_summary(artifact)['facts']}
        assert facts['result_rows']==facts['source_rows']==120000
        assert facts['taValue.max']==99
        assert artifact['preview_policy']['total_groups']==1000
        assert len(artifact['preview'])<=100
        result={'rows':120000,'columns':10,'groups':1000,'memory_limit':'512MB','threads':2,
                'export_and_preview_seconds':round(exported-started,4),
                'summary_seconds':round(ended-exported,4),'total_seconds':round(ended-started,4),
                'parquet_bytes':Path(artifact['path']).stat().st_size,
                'preview_rows':len(artifact['preview']),
                'scope':'仅已执行结果的导出、排序、覆盖预览和摘要；不含CSV导入、补充表pivot或模型调用'}
        (root/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__': main()
