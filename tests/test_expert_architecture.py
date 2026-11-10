import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_synthetic_queries as fixtures
from srs_helper.agent import DiagnosticEngine
from srs_helper.analysis_bundle import freeze_bundle, read_bundle
from srs_helper.expert_tools import run_tools
from srs_helper.services import ModelService, ServiceConfig
from srs_helper.plans import default_plan


class BundleTests(unittest.TestCase):
    def test_immutable_and_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload={'evidence':{'artifacts':[]},'state':{'goals':[],'steps':[]}}
            ref=freeze_bundle(tmp,payload)
            self.assertEqual(ref,freeze_bundle(tmp,payload))
            self.assertEqual(read_bundle(ref)['state'],payload['state'])
            Path(ref['path']).write_text('{}',encoding='utf-8')
            with self.assertRaises(ValueError): read_bundle(ref)
            with self.assertRaises(ValueError): freeze_bundle(tmp,payload)

    def test_tool_dedup_and_interrupted_task(self):
        actions=[]
        saved=[]
        params=dict(propose=lambda _: {'action':'search','search':'B','purpose':'检查规则'},
                    payload=lambda:{},query=lambda _:None,
                    search=lambda q:actions.append(q) or ['doc'],
                    save=lambda ledger:saved.append(json.loads(json.dumps(ledger))),
                    restored=[],remaining=lambda:10)
        result=run_tools(**params)
        self.assertEqual(actions,['B'])
        self.assertEqual(result[-1]['status'],'NO_PROGRESS')
        params['restored']=[{'status':'RUNNING'}]
        self.assertEqual(run_tools(**params)[-1]['status'],'FAILED')
        self.assertEqual(actions,['B'])


class ExpertIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.SyntheticQueryTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def test_frozen_retry_has_no_analysis_tools(self):
        engine=DiagnosticEngine(self.root/'frozen',self.root/'empty',model=ModelService(ServiceConfig()))
        sid=engine.store.create(self.normal_db)
        with patch('srs_helper.agent.report_index',side_effect=RuntimeError('report failure')):
            with self.assertRaises(RuntimeError): engine.run(sid,'查看增益',action_id='11111111-1111-4111-8111-111111111111')
        with patch('srs_helper.agent.execute_plan',side_effect=AssertionError('query after freeze')), \
             patch('srs_helper.agent.summarize',side_effect=AssertionError('summary after freeze')), \
             patch.object(engine.index,'search',side_effect=AssertionError('retrieval after freeze')):
            result=engine.run(sid,'查看增益',action_id='11111111-1111-4111-8111-111111111111',retry=True)
        self.assertEqual(result['steps'],['plan','agent_loop','explain','report'])
        self.assertEqual(read_bundle(result['evidence_bundle'])['analysis_id'],result['action_id'])
        self.assertTrue(result['report_index']['evidence'])

    def test_adaptive_query_then_finish(self):
        meta=self.normal_meta
        class Model:
            config=ServiceConfig(provider='local_openai',base_url='http://example',model='test')
            calls=0
            def chat_json(self,messages):
                if '分析工具调度器' in messages[0].content:
                    self.calls+=1
                    if self.calls==1:
                        return {'action':'query','purpose':'核查NI分布',
                                'query':default_plan(meta,'NI').model_dump(mode='json')}
                    return {'action':'finish','purpose':'已有统计'}
                if 'query_plan_schema' in messages[-1].content:
                    return default_plan(meta,'增益').model_dump(mode='json')
                return {'claims':[],'hypotheses':[]}
        model=Model()
        engine=DiagnosticEngine(self.root/'adaptive',self.root/'empty',model=model)
        result=engine.run(engine.store.create(self.normal_db),'查看增益')
        self.assertEqual(model.calls,2)
        self.assertEqual(result['tool_ledger'][0]['status'],'SUCCEEDED')
        self.assertTrue(any(a.get('origin')=='EXPERT_TOOL' for a in result['artifacts']))
        self.assertEqual(result['steps'],['plan','agent_loop','explain','report'])
