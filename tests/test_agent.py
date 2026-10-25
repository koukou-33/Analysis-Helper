import json
import sqlite3
import unittest
from pathlib import Path
from uuid import uuid4

import httpx
from langchain_core.messages import HumanMessage

import test_synthetic_queries as fixtures
from srs_helper.agent import DiagnosticEngine
from srs_helper.queries import Scope
from srs_helper.services import ModelService, ServiceConfig, ServiceError, extract_feature


class AgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.SyntheticQueryTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_graph_persistence_idempotency_and_missing_asset(self):
        storage = self.root/"agent1"
        engine = DiagnosticEngine(storage,self.root/"empty_assets",model=ModelService(ServiceConfig()))
        sid = engine.store.create(self.normal_db,"持久测试")
        aid = str(uuid4())
        first = engine.run(sid,"查看四流增益",Scope(logic_cell_id=0),aid)
        self.assertEqual(first["summary"]["flow_valid_count"],40)
        self.assertEqual(first["steps"],["retrieve","plan","execute","verify","explain"])
        reopened = DiagnosticEngine(storage,self.root/"empty_assets",model=ModelService(ServiceConfig()))
        same = reopened.run(sid,"查看四流增益",Scope(logic_cell_id=0),aid)
        self.assertEqual(same,first)
        with sqlite3.connect(reopened.store.db_path) as db:
            self.assertGreater(db.execute("SELECT count(*) FROM checkpoints").fetchone()[0],0)
        with self.assertRaises(ValueError):
            reopened.run(sid,"different",action_id=aid)
        Path(first["artifacts"][0]["path"]).unlink()
        history = reopened.store.history(sid)
        self.assertEqual(len(history[0]["missing_artifacts"]),1)
        self.assertEqual(history[0]["answer"],first["answer"])

    def test_model_invalid_plan_falls_back_without_losing_statistics(self):
        class InvalidModel:
            config = ServiceConfig(provider="local_openai",base_url="http://example",model="test")
            def chat_json(self,messages):
                return {"unexpected":"output"}
        engine = DiagnosticEngine(self.root/"agent2",self.root/"empty_assets",model=InvalidModel())
        sid = engine.store.create(self.normal_db)
        result = engine.run(sid,"test")
        self.assertEqual(result["summary"]["flow_valid_count"],80)
        self.assertTrue(any("修复失败" in n for n in result["notices"]))

    def test_http_auth_redaction_and_local_no_key(self):
        calls = []
        def reject(request):
            calls.append(request)
            return httpx.Response(401,text="SECRET_KEY")
        model = ModelService(ServiceConfig("openai_compatible","https://example.invalid/v1","test","SECRET_KEY"),httpx.MockTransport(reject))
        with self.assertRaises(ServiceError) as caught:
            model.chat_json([HumanMessage(content="hello")])
        self.assertNotIn("SECRET_KEY",str(caught.exception))
        self.assertEqual(len(calls),1)
        def success(request):
            self.assertNotIn("authorization",request.headers)
            return httpx.Response(200,json={"choices":[{"message":{"content":'{"ok":true}'}}]})
        model = ModelService(ServiceConfig("local_openai","http://localhost/v1","test"),httpx.MockTransport(success))
        self.assertEqual(model.chat_json([HumanMessage(content="hello")]),{"ok":True})

    def test_plan_feedback_and_explanation_diagnostics_persist(self):
        from srs_helper.plans import default_plan
        parent = self
        class RepairModel:
            config = ServiceConfig(provider='local_openai',base_url='http://example',model='test')
            calls = 0
            def chat_json(self,messages):
                self.calls += 1
                if self.calls == 1:
                    plan = default_plan(parent.normal_meta,'test').model_dump(mode='json')
                    plan['steps'] = [{'step_id':'s1','operator':'CONDITION_COUNT','condition':{'op':'eq','field':'gain_missing','value':True}}]
                    return plan
                if self.calls == 2:
                    parent.assertIn('CONDITION_COUNT',messages[-1].content)
                    return default_plan(parent.normal_meta,'test').model_dump(mode='json')
                return {'claims':[],'hypotheses':[{'text':'guess','references':['not_known']}]}
        engine = DiagnosticEngine(self.root/'agent_diagnostics',self.root/'empty_assets',model=RepairModel())
        sid = engine.store.create(self.normal_db)
        result = engine.run(sid,'test')
        self.assertEqual(len(result['validation_errors']),4)
        self.assertEqual(result['validation_errors'][-1]['code'],'UNBOUND_HYPOTHESIS')
        self.assertEqual(result['validation_errors'][-1]['location'],'hypotheses[0].references')
        self.assertTrue(any('UNBOUND_HYPOTHESIS' in n for n in result['notices']))
        self.assertEqual(engine.store.history(sid)[0]['validation_errors'],result['validation_errors'])

    def test_embedding_response_contract(self):
        model = ModelService(ServiceConfig("local_openai","http://localhost/v1","test"),httpx.MockTransport(
            lambda r:httpx.Response(200,json={"data":[{"index":0,"embedding":[1,2]},{"index":1,"embedding":[1]}]})))
        with self.assertRaises(ServiceError):
            model.embed(["a","b"])

    def test_feature_extraction_requires_literal_source(self):
        doc = {"path":"features/B.md","version":"hash1","body":"条件只是必要条件。"}
        class Mock:
            def chat_json(self,messages):
                return {"feature_id":"B","title":"B","flag_field":"featureB","conditions":[
                    {"condition_id":"c1","role":"NECESSARY","expression":{"op":"gt","field":"usrRsrp","value":-120},
                     "source":{"document_id":"features/B.md","revision":"hash1","section":"1","excerpt":"伪造的原文"}}]}
        with self.assertRaises(ServiceError):
            extract_feature(Mock(),doc,["usrRsrp"])
