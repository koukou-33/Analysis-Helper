import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pyarrow.parquet as pq
from srs_helper.pipeline import build_dataset
from srs_helper.agent import DiagnosticEngine
from srs_helper.services import ModelService, ServiceConfig
from srs_helper.queries import Scope
from srs_helper.provenance import record_trace, validate_parent
from srs_helper.database import open_dataset


class ContinuousAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        assets=self.root/'assets'; (assets/'features').mkdir(parents=True)
        original=Path('knowledge_assets/features/SPF特性相关分析指南.md').read_text(encoding='utf-8')
        (assets/'features'/'spf.md').write_text(original,encoding='utf-8')
        source=self.root/'NR_SRS.states.csv'
        # uid10: on -> off -> on; unknown breaks the next off; a quality-excluded observation breaks the next on.
        cases=[(10,0,1,1,4),(10,1,0,0,4),(10,2,1,1,4),(10,3,'',1,4),(10,4,0,0,4),
               (10,5,0,0,0),(10,6,1,1,4),
               # conflicting simultaneous observations break adjacency
               (10,7,0,0,4),(10,7,1,1,4),(10,8,0,0,4),
               # new observation session; no cross-session event
               (10,300,1,1,4),(20,0,0,0,4),(20,1,0,0,4)]
        with source.open('w',newline='') as f:
            writer=csv.writer(f)
            writer.writerow(['ueLayerNum','uwTtiCnt1','logicCellId','crnti','uwData','uwTime','srsSpfSwitch',
                'isAiranActInd','preSinr','spfPreSinrThd','codeUsrSpfSw','preSinrUeCurr','codeUsrSpfFlag'])
            for i,(ue,sec,flag,switch,layer) in enumerate(cases):
                writer.writerow([layer,i,0,ue,'2026-10-27',f'00:{sec//60:02d}:{sec%60:02d}.000',switch,1,-5,0,1,-5,flag])
        self.directory,self.meta=build_dataset(source,self.root/'data',json.loads(Path('config/input_contract.json').read_text()))
        self.engine=DiagnosticEngine(self.root/'sessions',assets,model=ModelService(ServiceConfig()))
        self.sid=self.engine.store.create(self.directory)

    def tearDown(self):
        import streamlit as st
        st.cache_resource.clear()
        self.tmp.cleanup()

    def test_states_and_complete_lineage(self):
        result=self.engine.run(self.sid,'SPF 用户状态变化原因')
        self.assertEqual(result['route'],'FEATURE')
        self.assertEqual(result['model_status']['plan'],'NOT_REQUIRED_STRUCTURED_FEATURE')
        self.assertEqual(result['session_states']['status'],'VERIFIED')
        self.assertEqual(result['session_states']['session_count'],3)
        self.assertEqual(result['session_states']['observed_changes'],2)
        events=next(a for a in result['artifacts'] if a['operator']=='OBSERVED_CHANGES')
        rows=pq.read_table(events['path']).to_pylist()
        self.assertEqual([(r['before_state'],r['after_state']) for r in rows],[('ENABLED','DISABLED'),('DISABLED','ENABLED')])
        self.assertTrue(all('c1' in r['changed_conditions'] for r in rows))
        trace=record_trace(self.directory,rows[0]['before_record_ids'][0])
        self.assertEqual(trace['file_row_id'],1)
        metric=result['feature']['actual_metrics']['enabled']
        self.assertEqual(metric['numerator'],5)
        self.assertEqual(metric['denominator'],12)
        self.assertEqual(metric['numerator_cohort_id'],result['feature']['cohorts']['enabled']['cohort_id'])
        self.assertTrue(events['parent_artifact_ids'])
        reason=result['feature']['reason_metrics']['c1']
        self.assertEqual(reason['disabled']['numerator'],6)
        self.assertEqual(reason['disabled']['denominator'],6)
        self.assertEqual(reason['all']['denominator'],12)
        self.assertIn('50.00%',result['answer'])

    def test_explicit_parent_scope_and_no_prose_dependency(self):
        first=self.engine.run(self.sid,'SPF 生效比例')
        # Replayed parent is read from immutable, hashed result, not any assistant narrative.
        child=self.engine.run(self.sid,'这些记录的 SPF 未生效条件',parent_action_id=first['action_id'],cohort_name='disabled')
        self.assertEqual(child['analysis_context']['parent_analysis_id'],first['action_id'])
        self.assertEqual(child['feature']['sample_cnt'],first['feature']['flag_states']['False'])
        self.assertEqual(child['feature']['flag_states'].get('True',0),0)
        self.assertEqual(child['feature']['actual_metrics']['enabled']['value'],0)
        self.assertEqual(child['summary']['business_valid_count'],6)
        self.assertTrue(all(a.get('input_cohort_id')==first['feature']['cohorts']['disabled']['cohort_id'] for a in child['artifacts'] if a.get('origin')=='FEATURE'))
        with self.assertRaisesRegex(ValueError,'scope or version'):
            self.engine.run(self.sid,'SPF',Scope(crnti=20),parent_action_id=first['action_id'],cohort_name='disabled')
        with self.assertRaises(ValueError):
            self.engine.run(self.sid,'这些用户为什么未生效')
        from copy import deepcopy
        wrong_unit=deepcopy(first)
        wrong_unit['analysis_context']['analysis_unit']='session'
        with self.assertRaisesRegex(ValueError,'unit mismatch'):
            validate_parent(wrong_unit,self.meta,Scope(),'disabled')
        wrong=dict(self.meta,dataset_fingerprint='wrong')
        with self.assertRaises(ValueError):
            validate_parent(first,wrong,Scope(),'disabled')
        a=next(a for a in first['artifacts'] if a['artifact_id']==first['feature']['cohorts']['disabled']['artifact_id'])
        Path(a['path']).write_bytes(b'changed')
        with self.assertRaises(ValueError):
            self.engine.run(self.sid,'SPF',parent_action_id=first['action_id'],cohort_name='disabled')

    def test_user_reason_ratio_is_explicitly_deferred(self):
        result=self.engine.run(self.sid,'SPF 未生效原因占总用户的比例')
        self.assertEqual(next(g for g in result['goals'] if g['id']=='user_reason_ratio')['status'],'NOT_RUN')
        self.assertEqual(result['status'],'PARTIAL_FACTS_READY')
        self.assertIn('用户级原因占比尚未启用',result['answer'])

    def test_inherited_unknown_has_no_false_transitions_and_rule_change_rejected(self):
        first=self.engine.run(self.sid,'SPF 用户状态变化')
        child=self.engine.run(self.sid,'SPF 用户状态变化',parent_action_id=first['action_id'],cohort_name='unknown')
        self.assertEqual(child['session_states']['observed_changes'],0)
        self.assertEqual(child['feature']['sample_cnt'],1)
        path=self.root/'assets'/'features'/'spf.md'
        # Change the executable rule, not just narrative.
        text=path.read_text(encoding='utf-8').replace('revision: 1','revision: 2',1)
        path.write_text(text,encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'rule changed'):
            self.engine.run(self.sid,'SPF',parent_action_id=first['action_id'],cohort_name='all')

    def test_ui_explicit_followup_selection(self):
        from streamlit.testing.v1 import AppTest
        import streamlit as st
        st.cache_resource.clear()
        first=self.engine.run(self.sid,'SPF 生效比例')
        with patch('srs_helper.agent.DiagnosticEngine',return_value=self.engine):
            app=AppTest.from_file('app.py',default_timeout=30).run()
            self.assertFalse(app.exception)
            next(s for s in app.selectbox if s.label=='追问来源').set_value(first['action_id']).run()
            next(s for s in app.selectbox if s.label=='继承的记录群体').set_value('disabled').run()
            next(t for t in app.text_area if t.label=='问题描述').set_value('这些记录的 SPF 未生效条件')
            next(b for b in app.button if b.label=='开始分析').click().run()
            self.assertFalse(app.exception)
            latest=self.engine.store.history(self.sid)[-1]
            self.assertEqual(latest['analysis_context']['parent_analysis_id'],first['action_id'])
            self.assertEqual(latest['feature']['sample_cnt'],6)

    def test_event_followup_binds_exact_before_and_after(self):
        first=self.engine.run(self.sid,'SPF 用户变化')
        artifact=next(a for a in first['artifacts'] if a['operator']=='OBSERVED_CHANGES')
        event=pq.read_table(artifact['path']).to_pylist()[0]
        child=self.engine.run(self.sid,'这些变化的 SPF 条件',parent_action_id=first['action_id'],parent_event_id=event['event_id'])
        self.assertEqual(child['feature']['sample_cnt'],2)
        self.assertEqual(child['analysis_context']['parent_event_id'],event['event_id'])
        with self.assertRaises(ValueError):
            self.engine.run(self.sid,'这些变化',parent_action_id=first['action_id'],cohort_name='all')

    def test_resource_only_plan_does_not_expand_stream(self):
        from srs_helper.plans import QueryPlan, Step, execute_plan, prepare_scope
        seen=[]
        def spy(con,scope,required_fields=(),include_stream=True):
            seen.append(include_stream)
            return prepare_scope(con,scope,required_fields,include_stream)
        plan=QueryPlan(dataset_id=self.meta['dataset_id'],dataset_fingerprint=self.meta['dataset_fingerprint'],question='count',
            steps=[Step(step_id='s1',operator='GROUP_AGG',metrics=[{'field':'record_id','aggregate':'count','output':'n'}])])
        with patch('srs_helper.plans.prepare_scope',side_effect=spy):
            result=execute_plan(self.directory,Scope(),plan,self.root/'no_stream')
        self.assertEqual(seen,[False])
        self.assertEqual(result[-1]['preview'][0]['n'],12)
        with open_dataset(self.directory) as (con,_):
            temp=con.execute("SELECT current_setting('temp_directory')").fetchone()[0]
            self.assertLess(len(temp),180)
