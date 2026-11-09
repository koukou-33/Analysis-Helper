import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from srs_helper.sessions import SessionStore
from srs_helper.progress import ActionProgress, StorageFailure
from srs_helper.preview import CoveragePreview
from srs_helper.result_summary import summarize_relation, read_summary, model_evidence, bounded_explanation_context
from srs_helper.plans import QueryPlan, Step, Expression, export_relation
from srs_helper.queries import Scope
from srs_helper.explanation import validate_explanation, ExplanationError
from srs_helper.chart_preview import chart_objects, time_coverage
from srs_helper.question_scope import parse_question


class ReliabilityTests(unittest.TestCase):
    def test_transactional_progress_restore_conflict_and_no_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=SessionStore(tmp); sid=str(uuid4()); aid=str(uuid4()); folder=store.directory(sid)
            folder.mkdir(parents=True)
            store.begin(sid,aid,{'question':'test'})
            progress=ActionProgress(store,sid,aid,folder)
            stale=ActionProgress(store,sid,aid,folder)
            with patch('os.replace',side_effect=PermissionError('Excel holds unrelated file')):
                progress.save(model_calls=2,budget_started=123)
            resumed=ActionProgress(store,sid,aid,folder)
            self.assertEqual(resumed.data,{'model_calls':2,'budget_started':123})
            with self.assertRaises(StorageFailure): stale.save(model_calls=0)
            blocker=sqlite3.connect(store.db_path)
            try:
                blocker.execute('BEGIN IMMEDIATE')
                with self.assertRaises(StorageFailure): resumed.save(model_calls=3)
                self.assertEqual(resumed.data['model_calls'],2)
            finally:
                blocker.rollback(); blocker.close()
            resumed.save(model_calls=3)
            self.assertEqual(ActionProgress(store,sid,aid,folder).data['model_calls'],3)

    def test_legacy_migration_and_corrupt_budget_never_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=SessionStore(tmp); sid=str(uuid4()); aid=str(uuid4()); folder=store.directory(sid)
            folder.mkdir(parents=True); store.begin(sid,aid,{})
            legacy=folder/('progress_'+hashlib.sha256(aid.encode()).hexdigest()+'.json')
            legacy.write_text('{"model_calls":7,"budget_started":100}',encoding='utf-8')
            progress=ActionProgress(store,sid,aid,folder)
            self.assertEqual(progress.data['model_calls'],7)
            legacy.write_text('broken',encoding='utf-8')
            self.assertEqual(ActionProgress(store,sid,aid,folder).data['model_calls'],7)
            with store.db() as db: db.execute("UPDATE action_progress SET payload='broken'")
            with self.assertRaises(StorageFailure): ActionProgress(store,sid,aid,folder)

    def test_coverage_sampling_is_repeatable_and_spans_time(self):
        rows=[{'logicCellId':i//200,'ue_uid':str(i//200),'event_ts':f'{i:06d}','record_id':i,'value':i} for i in range(1000)]
        left=CoveragePreview(list(rows[0])); right=CoveragePreview(list(rows[0]))
        for row in rows: left.add(row)
        for row in reversed(rows): right.add(row)
        self.assertEqual(left.rows(),right.rows())
        values={r['value'] for r in left.rows()}
        self.assertTrue({0,199,800,999}<=values)
        self.assertLessEqual(len(left.rows()),100)

    def test_full_summary_filter_counts_units_claims_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp, duckdb.connect(config={'threads':'2'}) as con:
            con.execute('CREATE TABLE _etl_meta(metadata VARCHAR)')
            con.execute('INSERT INTO _etl_meta VALUES (?)',[json.dumps({'contract':{}})])
            con.execute('CREATE TABLE resource AS SELECT i AS record_id,i%2 AS logicCellId, i::DOUBLE AS taValue FROM range(150) t(i)')
            con.execute('UPDATE resource SET taValue=NULL WHERE record_id=149')
            con.execute('CREATE TABLE s1 AS SELECT * FROM resource WHERE taValue>100')
            step=Step(step_id='s1',operator='FILTER',condition=Expression(op='gt',field='taValue',value=100))
            plan=QueryPlan(dataset_id='d',dataset_fingerprint='f',question='TA',steps=[step],limitations=['测试限制'])
            a=export_relation(con,'s1',Path(tmp),plan,Scope(),'FILTER')
            summarize_relation(con,'s1',a,step,{'taValue':'raw'})
            facts={f['fact_id']:f for f in read_summary(a)['facts']}
            self.assertEqual(facts['source_rows']['value'],150)
            self.assertEqual(facts['excluded_unknown']['value'],1)
            self.assertEqual(facts['excluded_false']['value'],101)
            self.assertEqual(facts['taValue.max']['value'],148)
            claim={'artifact_id':a['artifact_id'],'row_key':{'fact_id':'taValue.max'},'column':'value','value':148,'unit':'raw'}
            validate_explanation({'claims':[claim],'hypotheses':[]},[a],[])
            with self.assertRaises(ExplanationError):
                validate_explanation({'claims':[{**claim,'unit':'dB'}],'hypotheses':[]},[a],[])
            payload=model_evidence([a]); self.assertTrue(payload['artifacts'][0]['summary'])
            limited=bounded_explanation_context({'question':'q','evidence':payload,'knowledge':['x'*70000]})
            self.assertLessEqual(len(limited),64000)
            self.assertIn('knowledge',json.loads(limited)['context_omissions'])
            Path(a['summary_ref']['path']).write_text('{}')
            with self.assertRaises(ValueError): read_summary(a)

    def test_chart_full_time_extrema_and_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'points.parquet'
            start=datetime(2026,11,9)
            rows=[{'logicCellId':1,'ue_uid':'u','event_ts':start+timedelta(seconds=i),
                   'segment':0 if i<2500 else 1,'barrier':i==2500,'value':None if i==2500 else 9999. if i==2999 else 1.} for i in range(3000)]
            pq.write_table(pa.Table.from_pylist(list(reversed(rows))),path)
            artifact={'path':str(path)}
            self.assertEqual(chart_objects(artifact),[('1','u')])
            frame,coverage=time_coverage(artifact,'1','u',limit=100)
            self.assertEqual(coverage['total_points'],3000)
            self.assertLessEqual(len(frame),100)
            self.assertEqual(frame['value'].max(),9999.)
            self.assertEqual(frame.event_ts.min(),start)
            self.assertEqual(frame.event_ts.max(),start+timedelta(seconds=2999))
            self.assertTrue(frame.event_ts.is_monotonic_increasing)
            self.assertTrue(frame.barrier.any())

    def test_shared_intent_and_explicit_identity_candidate(self):
        text='所有crnti实际都是一个用户，不同接入后不同的crnti，他在不同小区的TA变化怎么样'
        result=parse_question(text,Scope())
        self.assertTrue(result['single_subject_candidate']); self.assertTrue(result['trend'])
        self.assertIsNone(result['scope']['crnti'])
        for text in ('TA随时间怎么走','TA有没有突变','TA trend over time'):
            self.assertTrue(parse_question(text,Scope())['trend'])
        self.assertFalse(parse_question('不要趋势，只看分布',Scope())['trend'])
