"""Synthetic end-to-end lineage benchmark, separate from real-data acceptance."""
import argparse
import csv
import json
import os
import time
from pathlib import Path
from uuid import uuid4
from srs_helper.pipeline import build_dataset
from srs_helper.agent import DiagnosticEngine
from srs_helper.services import ModelService, ServiceConfig
from srs_helper.performance import rss_bytes


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--records',type=int,default=30000)
    parser.add_argument('--extra-fields',type=int,default=0)
    args=parser.parse_args()
    if args.records<1 or args.extra_fields<0: raise ValueError('Invalid benchmark size')
    root=Path('workspace_storage/continuous_benchmark')/str(uuid4());root.mkdir(parents=True)
    source=root/'NR_SRS.benchmark.csv'
    with source.open('w',newline='') as f:
        writer=csv.writer(f)
        writer.writerow(['ueLayerNum','uwTtiCnt1','logicCellId','crnti','uwData','uwTime','srsSpfSwitch',
            'isAiranActInd','preSinr','spfPreSinrThd','codeUsrSpfSw','preSinrUeCurr','codeUsrSpfFlag']+[f'extra_{j}' for j in range(args.extra_fields)])
        for i in range(args.records):
            sec=i%30; state=1 if sec<10 or sec>=20 else 0
            writer.writerow([4,i,0,100+i//30,'2026-10-27',f'00:00:{sec:02d}.000',state,1,-5,0,1,-5,state]+[i%10]*args.extra_fields)
    started=time.perf_counter()
    directory,meta=build_dataset(source,root/'data',json.loads(Path('config/input_contract.json').read_text()))
    load_seconds=time.perf_counter()-started
    assets=root/'assets';(assets/'features').mkdir(parents=True)
    (assets/'features'/'spf.md').write_text(Path('knowledge_assets/features/SPF特性相关分析指南.md').read_text(encoding='utf-8'),encoding='utf-8')
    os.environ['EMBEDDING_PROVIDER']='';os.environ['VISION_PROVIDER']=''
    os.environ['SRS_ANALYSIS_BUDGET_SECONDS']='600'
    engine=DiagnosticEngine(root/'sessions',assets,model=ModelService(ServiceConfig()))
    sid=engine.store.create(directory)
    started=time.perf_counter();first=engine.run(sid,'SPF 用户状态变化原因');analysis_seconds=time.perf_counter()-started
    if first['feature']['sample_cnt']!=args.records or first['session_states']['status']!='VERIFIED': raise AssertionError('Incomplete benchmark')
    started=time.perf_counter();child=engine.run(sid,'这些记录的 SPF 未生效条件',parent_action_id=first['action_id'],cohort_name='disabled');followup_seconds=time.perf_counter()-started
    if child['feature']['sample_cnt']!=first['feature']['flag_states'].get('False',0): raise AssertionError('Population mismatch')
    result={'records':args.records,'extra_fields':args.extra_fields,'load_seconds':load_seconds,'analysis_seconds':analysis_seconds,
            'followup_seconds':followup_seconds,'observed_changes':first['session_states']['observed_changes'],
            'sessions':first['session_states']['session_count'],'rss_end_bytes':rss_bytes(),
            'supplemental_rows':sum(f.get('supplemental_numeric_rows',0) for f in meta['files']),
            'artifact_bytes':sum(Path(a['path']).stat().st_size for a in first['artifacts']),
            'result_path':str(root/'results.json'),'note':'Synthetic; RSS is endpoint only; no model calls; not full production acceptance'}
    (root/'results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))

if __name__=='__main__': main()
