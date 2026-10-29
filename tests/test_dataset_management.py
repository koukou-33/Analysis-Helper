import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import portalocker
from streamlit.testing.v1 import AppTest
from srs_helper.pipeline import build_dataset
from srs_helper.agent import DiagnosticEngine
from srs_helper.services import ModelService, ServiceConfig


class DatasetManagementTests(unittest.TestCase):
    def setUp(self):
        import streamlit as st
        st.cache_resource.clear()
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.source=self.root/'NR_SRS.csv'
        with self.source.open('w',newline='') as f:
            w=csv.writer(f)
            w.writerow(['ueLayerNum','uwTtiCnt1','logicCellId','crnti','uwData','uwTime'])
            w.writerow([4,1,0,10,'2026-10-29','00:00:00.000'])
        self.engine=DiagnosticEngine(self.root/'store',self.root/'assets',model=ModelService(ServiceConfig()))
        self.store=self.engine.store
        self.directory,self.meta=build_dataset(self.source,self.store.root/'datasets',json.loads(Path('config/input_contract.json').read_text()))

    def tearDown(self):
        import streamlit as st
        st.cache_resource.clear()
        self.tmp.cleanup()

    def test_name_persistence_rename_and_immutable_manifest(self):
        before=(self.directory/'input_manifest.json').read_bytes()
        self.assertTrue(self.store.datasets()[0]['name'].startswith('未命名数据'))
        self.store.name_dataset(self.directory,'南京站点 SPF 开启')
        sid=self.store.create(self.directory)
        self.assertEqual(self.store.get(sid)['title'],'南京站点 SPF 开启')
        self.store.name_dataset(self.directory,'外场复验')
        from srs_helper.sessions import SessionStore
        reopened=SessionStore(self.store.root)
        self.assertEqual(reopened.datasets()[0]['name'],'外场复验')
        self.assertEqual(reopened.datasets()[0]['sessions'],1)
        self.assertEqual(before,(self.directory/'input_manifest.json').read_bytes())
        for invalid in ('','  ','bad\nname','x'*101):
            with self.assertRaises(ValueError):
                self.store.name_dataset(self.directory,invalid)

    def test_delete_preserves_history_and_original_csv(self):
        from uuid import uuid4
        sid=self.store.create(self.directory); aid=str(uuid4())
        self.store.begin(sid,aid,{'question':'test'})
        self.store.finish(sid,aid,{'answer':'保留历史报告','artifacts':[]})
        self.store.delete_dataset(self.directory,expected_dataset_id=self.meta['dataset_id'])
        self.assertFalse(self.directory.exists())
        self.assertTrue(self.source.exists())
        self.assertEqual(self.store.datasets(),[])
        self.assertEqual(self.store.history(sid)[0]['answer'],'保留历史报告')
        with self.assertRaises(Exception):
            self.store.create(self.directory)

    def test_busy_dataset_and_wrong_identity_cannot_be_deleted(self):
        with self.store.dataset_guard(self.directory):
            with self.assertRaises(portalocker.exceptions.LockException):
                self.store.delete_dataset(self.directory,expected_dataset_id=self.meta['dataset_id'])
        with self.assertRaises(ValueError):
            self.store.delete_dataset(self.directory,expected_dataset_id='wrong')
        self.assertTrue((self.directory/'analysis.duckdb').exists())

    def test_outside_directory_and_root_rejected(self):
        for path in (self.root,self.store.root/'datasets',self.source):
            with self.assertRaises(ValueError):
                self.store.delete_dataset(path,expected_dataset_id=self.meta['dataset_id'])
        self.assertTrue(self.source.exists())

    def test_ui_load_named_data_and_explicit_delete(self):
        with patch('srs_helper.agent.DiagnosticEngine',return_value=self.engine):
            app=AppTest.from_file('app.py',default_timeout=30).run()
            next(t for t in app.text_input if t.label=='数据名称').set_value('外场 A')
            next(t for t in app.text_input if t.label=='目录或单个 CSV 路径').set_value(str(self.source))
            next(s for s in app.selectbox if s.label=='输入契约').set_value('config/input_contract.json')
            next(b for b in app.button if b.label=='加载新数据版本').click().run()
            self.assertFalse(app.exception)
            named=next(d for d in self.store.datasets() if d['name']=='外场 A')
            next(s for s in app.selectbox if s.label=='固定数据版本').set_value(named['path']).run()
            self.assertTrue(next(b for b in app.button if b.label=='删除数据集').disabled)
            next(t for t in app.text_input if t.label=='修改数据名称').set_value('外场 B')
            next(b for b in app.button if b.label=='保存数据名称').click().run()
            self.assertEqual(self.store.dataset_name(named['path']),'外场 B')
            self.assertEqual(next(s for s in app.selectbox if s.label=='固定数据版本').value,named['path'])
            next(c for c in app.checkbox if c.label=='确认删除此数据集及其输入快照').check().run()
            next(b for b in app.button if b.label=='删除数据集').click().run()
            self.assertFalse(app.exception)
            self.assertFalse(Path(named['path']).exists())
            self.assertTrue(self.source.exists())
