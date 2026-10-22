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
            next(r for r in app.radio if r.label=="视图").set_value("查询证据").run()
            self.assertFalse(app.exception)
            self.assertTrue(any(b.label=="执行探索 SQL" for b in app.button))
            next(r for r in app.radio if r.label=="视图").set_value("案例审核").run()
            next(b for b in app.button if b.label=="从当前结果提炼案例草稿").click().run()
            self.assertFalse(app.exception)
            next(t for t in app.text_input if t.label=="审核人").set_value("Test reviewer")
            next(c for c in app.checkbox if "同意发布" in c.label).check()
            next(b for b in app.button if b.label=="审核并发布").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(len(engine.index.documents()),1)
