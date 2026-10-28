"""Reuse a synthetic snapshot to measure real multi-round verification, without API calls.

Run: python -m scripts.benchmark_verification --dataset <immutable dataset dir>
Optional --long-table points to the independent scope_benchmark/bench.duckdb fixture.
"""
import argparse
import json
import os
import time
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4
import duckdb
from srs_helper.agent import DiagnosticEngine
from srs_helper.performance import rss_bytes
from srs_helper.services import ServiceConfig
from srs_helper.plans import prepare_scope
from srs_helper.queries import Scope
from srs_helper.provenance import active_cohort


class CounterexampleModel:
    config=ServiceConfig(provider='local_openai',base_url='http://unused.invalid',model='offline-counterexample')
    def chat_json(self,messages):
        payload=json.loads(messages[1].content)
        if 'response_schema' not in payload:
            return {'claims':[],'hypotheses':[]}
        assessments=payload['assessments']
        if not assessments:
            return {'tasks':[{'goal_id':'disabled_conditions','cohort':'disabled','quantifier':'ALL',
                'expression':{'op':'eq','field':'srsSpfSwitch','value':1}}]}
        if len(assessments)==1:
            assert assessments[0]['status']=='REFUTED'
            return {'tasks':[{'goal_id':'disabled_conditions','cohort':assessments[0]['residual_cohorts']['false'],
                'quantifier':'ALL','expression':{'op':'le','field':'preSinr','value_field':'spfPreSinrThd'}}]}
        return {'tasks':[]}


def timed(fn):
    peak=rss_bytes() or 0; done=Event()
    def sample():
        nonlocal peak
        while not done.wait(.1):
            peak=max(peak,rss_bytes() or 0)
    worker=Thread(target=sample,daemon=True); worker.start()
    start=time.perf_counter()
    try:
        value=fn()
    finally:
        elapsed=time.perf_counter()-start; done.set(); worker.join()
    return value,{'seconds':elapsed,'sampled_peak_rss_bytes':peak}


def long_table(path):
    # Read-only fixture; temp tables and Arrow populations only. No ETL or persistent mutation.
    with duckdb.connect(str(path),read_only=True,config={'memory_limit':'512MB','threads':'2'}) as con:
        n=con.execute('SELECT count(*) FROM l2_extra_numeric').fetchone()[0]
        results=[]
        for i in range(2):
            population=con.execute('SELECT record_id FROM l4_ue_resource_ts WHERE CAST(record_id AS INTEGER)%2=0').fetch_arrow_table()
            con.register('selected_cohort',population)
            token=active_cohort.set(population)
            fields={'field7'} if i==0 else {'field7','field8'}
            try:
                _,metrics=timed(lambda:prepare_scope(con,Scope(),fields,include_stream=False))
                count=con.execute('SELECT count(*) FROM resource').fetchone()[0]
                assert count==len(population)
                metrics.update(fields=sorted(fields),cohort_records=count)
                results.append(metrics)
            finally:
                active_cohort.reset(token)
            con.execute('DROP TABLE resource'); con.execute('DROP TABLE IF EXISTS extra_values')
        return {'long_rows':n,'queries':results,'note':'Independent warm-cache narrow-read test; not a full production loop'}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--dataset',type=Path,required=True)
    parser.add_argument('--long-table',type=Path)
    args=parser.parse_args()
    root=Path('workspace_storage/verification_benchmark')/str(uuid4())
    assets=root/'assets'; (assets/'features').mkdir(parents=True)
    (assets/'features'/'spf.md').write_text(Path('knowledge_assets/features/SPF特性相关分析指南.md').read_text(encoding='utf-8'),encoding='utf-8')
    os.environ['EMBEDDING_PROVIDER']=''; os.environ['VISION_PROVIDER']=''
    os.environ['SRS_ANALYSIS_BUDGET_SECONDS']='600'
    engine=DiagnosticEngine(root/'sessions',assets,model=CounterexampleModel())
    sid=engine.store.create(args.dataset)
    baseline,base_time=timed(lambda:engine.run(sid,'SPF 生效比例'))
    result,loop_time=timed(lambda:engine.run(sid,'SPF 未生效原因'))
    loop=result['verification_loop']
    assert loop['round']==2 and loop['stop_reason']=='GOALS_VERIFIED'
    assert [a['status'] for a in loop['assessments']]==['REFUTED','SUPPORTED']
    data={'records':result['feature']['sample_cnt'],'baseline':base_time,'verification':loop_time,
        'loop_queries':loop['round'],'planner_calls':loop['planner_calls'],
        'verification_artifact_bytes':sum(Path(a['path']).stat().st_size for a in result['artifacts'] if a.get('origin')=='VERIFICATION_LOOP'),
        'verification_artifact_count':sum(a.get('origin')=='VERIFICATION_LOOP' for a in result['artifacts']),
        'action_id':result['action_id'],'result_dir':str(root.resolve()),
        'note':'Synthetic reused snapshot, sequential warm-cache runs, offline scripted planner; excludes ETL and real model latency'}
    if args.long_table:
        data['independent_long_table']=long_table(args.long_table)
    (root/'results.json').write_text(json.dumps(data,indent=2),encoding='utf-8')
    print(json.dumps(data))


if __name__=='__main__':
    main()
