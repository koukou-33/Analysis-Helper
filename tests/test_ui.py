import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import test_synthetic_queries as fixtures
from srs_helper.agent import DiagnosticEngine
from srs_helper.services import ModelService, ServiceConfig


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.SyntheticQueryTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_analyze_evidence_and_review_flow(self):
        engine = DiagnosticEngine(self.root/"ui",self.root/"knowledge",model=ModelService(ServiceConfig()))
        sid = engine.store.create(self.normal_db,"UI test")
        with patch("srs_helper.agent.DiagnosticEngine",return_value=engine):
            app = AppTest.from_file("app.py",default_timeout=30).run()
            self.assertFalse(app.exception)
            next(b for b in app.button if b.label=="开始分析").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(len(engine.store.history(sid)),1)
            next(r for r in app.selectbox if r.label=="选择画布内容").set_value("查询证据").run()
            self.assertFalse(app.exception)
            self.assertFalse(any(b.label=="执行探索 SQL" for b in app.button))
            next(c for c in app.checkbox if c.label=="显示技术详情与高级查询").check().run()
            self.assertTrue(any(b.label=="执行探索 SQL" for b in app.button))
            next(r for r in app.selectbox if r.label=="选择画布内容").set_value("案例审核").run()
            next(b for b in app.button if b.label=="从当前结果提炼案例草稿").click().run()
            self.assertFalse(app.exception)
            next(t for t in app.text_input if t.label=="审核人").set_value("Test reviewer")
            next(c for c in app.checkbox if "同意发布" in c.label).check()
            next(b for b in app.button if b.label=="审核并发布").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(len(engine.index.documents()),1)

    def test_feature_ratio_and_observation_canvas(self):
        import streamlit as st
        import pyarrow as pa
        from uuid import uuid4
        from srs_helper.queries import Scope
        from srs_helper.provenance import save_table
        st.cache_resource.clear()
        engine=DiagnosticEngine(self.root/'chart_ui',self.root/'chart_knowledge',model=ModelService(ServiceConfig()))
        sid=engine.store.create(self.normal_db,'图表测试')
        aid=str(uuid4())
        result=engine.run(sid,'查看变化',Scope(),aid)
        result['feature']={'validation':{'status':'VERIFIED'},'sample_cnt':10,
                           'flag_states':{'True':3,'False':2,'None':5}}
        artifact=save_table(pa.Table.from_pylist([
            {'ue_uid':'u1','event_ts':'2026-10-29T12:00:00','state':'ENABLED'},
            {'ue_uid':'u1','event_ts':'2026-10-29T12:01:00','state':'UNKNOWN'}]),
            engine.store.directory(sid)/'artifacts',result['artifacts'][0],'SESSION_OBSERVATIONS')
        result['artifacts'].append(artifact)
        engine.store.finish(sid,aid,result)
        with patch('srs_helper.agent.DiagnosticEngine',return_value=engine):
            app=AppTest.from_file('app.py',default_timeout=30).run()
            self.assertFalse(app.exception)
            self.assertGreaterEqual(len(app.get('plotly_chart')),2)
            self.assertTrue(any('包含状态未知条目' in c.value for c in app.caption))
        st.cache_resource.clear()
