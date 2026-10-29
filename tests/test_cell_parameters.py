import csv
import json
import tempfile
import unittest
from pathlib import Path
import pyarrow.parquet as pq
from srs_helper.pipeline import build_dataset
from srs_helper.database import open_dataset
from srs_helper.plans import QueryPlan,Step,Expression,execute_plan
from srs_helper.queries import Scope
from srs_helper.field_metadata import compile_mapping
from srs_helper.evidence import feature_analysis
from srs_helper.knowledge import parse_document
from srs_helper.reporting import feature_report
from srs_helper.provenance import record_trace, evidence_contains_record


class CellParameterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()
    def build(self,rows):
        source=self.root/'NR_SRS.csv'
        with source.open('w',newline='') as f:
            w=csv.writer(f)
            w.writerow(['ueLayerNum','uwTtiCnt1','logicCellId','crnti','uwData','uwTime',
                'srsSpfSwitch','spfPreSinrThd','spfPreSinr3D4DThd','isAiranActInd','preSinr','codeUsrSpfSw','preSinrUeCurr'])
            for i,(cell,ue,day,sec,value,threshold,model_threshold) in enumerate(rows):
                w.writerow([4,i,cell,ue,f'2026-10-{day:02d}',f'00:00:{sec:02d}.000',value,threshold,model_threshold,1,-5,1,-5])
        return build_dataset(source,self.root/'data',json.loads(Path('config/input_contract.json').read_text()))
    def query(self,directory,meta,scope=Scope()):
        plan=QueryPlan(dataset_id=meta['dataset_id'],dataset_fingerprint=meta['dataset_fingerprint'],question='SPF',
            steps=[Step(step_id='s1',operator='CONDITION_COUNT',conditions={'c0':Expression(op='eq',field='srsSpfSwitch',value=1)})])
        artifacts=execute_plan(directory,scope,plan,self.root/'artifacts')
        return pq.read_table(artifacts[0]['path']).to_pylist(),artifacts

    def test_metadata_column_duplicates_and_legacy_compatible(self):
        doc=Path('knowledge_assets/field_mappings/nr_srs_field_mapping.md')
        catalog=compile_mapping(doc)
        for name in ('srsSpfSwitch','spfPreSinrThd','spfPreSinr3D4DThd'):
            self.assertEqual(catalog['fields'][name]['data_level'],'小区级')
        custom=self.root/'mapping.md'
        custom.write_text('| 字段名称 | 含义 | 归属 | 数据层级 |\n| x | 参数 | L1内部 | 小区级 |\n| x | 参数 | L1内部 | 用户级 |',encoding='utf-8')
        result=compile_mapping(custom)
        self.assertIsNone(result['fields']['x']['data_level'])
        self.assertEqual(result['fields']['x']['responsibility_layer'],'L1内部')
        custom.write_text('| 字段名称 | 含义 | 归属 |\n| x | 参数 | L1内部 |',encoding='utf-8')
        self.assertIsNone(compile_mapping(custom)['fields']['x']['data_level'])

    def test_equal_brackets_cross_users_and_unlimited_gap_with_lineage(self):
        d,m=self.build([(0,10,1,0,1,0,-10),(0,20,15,0,'','',''),(0,30,29,0,1,0,-10)])
        rows,artifacts=self.query(d,m,Scope(crnti=20))
        self.assertEqual(len(rows),1)
        self.assertEqual([a['step_id'] for a in artifacts],['s1_rows','s1'])
        row=rows[0]
        self.assertEqual(row['srsSpfSwitch'],1)
        self.assertIsNone(row['__cell_raw_srsSpfSwitch'])
        self.assertEqual(artifacts[0]['imputed_record_count'],1)
        sources=row['__cell_fill_srsSpfSwitch']
        self.assertEqual(sources['method'],'BRACKET_EQUAL')
        self.assertNotEqual(sources['previous_record_id'],sources['next_record_id'])
        self.assertEqual(record_trace(d,sources['previous_record_id'])['file_row_id'],1)
        self.assertTrue(evidence_contains_record(artifacts[0],sources['previous_record_id']))
        self.assertFalse(evidence_contains_record(artifacts[0],'unrelated_record'))
        with open_dataset(d) as (con,_):
            self.assertEqual(con.execute('SELECT count(*) FROM cell_parameter_fills').fetchone()[0],3)
            self.assertEqual(con.execute("SELECT count(*) FROM l2_extra_numeric WHERE field_name='srsSpfSwitch'").fetchone()[0],2)
        allrows,_=self.query(d,m)
        self.assertEqual(next(r for r in allrows if r['crnti']==20)['srsSpfSwitch'],1)

    def test_switch_boundaries_one_sided_missing_and_cell_isolation(self):
        d,m=self.build([(0,10,1,0,'',0,0),(0,10,1,1,0,0,0),(0,10,1,2,'',0,0),
                        (0,10,1,3,1,0,0),(0,10,1,4,'',0,0),(1,10,1,2,'',0,0)])
        rows,_=self.query(d,m)
        self.assertEqual(sum(r['srsSpfSwitch'] is None for r in rows),4)
        self.assertTrue(all(r['__cell_fill_srsSpfSwitch'] is None for r in rows))

    def test_same_time_conflict_blocks_even_a_majority_and_invalid_is_barrier(self):
        d,m=self.build([(0,10,1,0,1,0,0),(0,10,1,1,'',0,0),
            (0,10,1,2,1,0,0),(0,11,1,2,1,0,0),(0,12,1,2,0,0,0),
            (0,13,1,2,'',0,0),(0,10,1,3,'',0,0),(0,10,1,4,1,0,0),
            (0,10,1,5,'bad',0,0),(0,10,1,6,'',0,0),(0,10,1,7,1,0,0)])
        rows,artifacts=self.query(d,m)
        self.assertEqual(artifacts[0]['imputed_record_count'],0)
        self.assertEqual(sum(r['srsSpfSwitch'] is None for r in rows),5)

    def test_feature_reports_original_and_inferred_conditions(self):
        d,m=self.build([(0,10,1,0,1,0,-10),(0,20,1,1,'','',''),(0,30,1,2,1,0,-10)])
        spec=parse_document(Path('knowledge_assets/features/SPF特性相关分析指南.md'),'feature')['metadata']
        result=feature_analysis(d,Scope(),m,spec,self.root/'evidence')
        self.assertEqual(result['activation_states']['True'],3)
        inference=result['cell_parameter_inference']
        self.assertEqual(inference['imputed_record_count'],1)
        self.assertEqual(inference['original_activation_states']['None'],1)
        self.assertIn('推定补齐',feature_report(result))
        self.assertEqual(result['conditions']['c1']['True'],3)
        self.assertEqual(inference['original_conditions']['c1']['None'],1)

    def test_same_timestamp_equal_observations_share_value(self):
        d,m=self.build([(0,10,1,0,1,0,-10),(0,20,1,0,'','','')])
        rows,_=self.query(d,m,Scope(crnti=20))
        self.assertEqual(rows[0]['srsSpfSwitch'],1)
        source=rows[0]['__cell_fill_srsSpfSwitch']
        self.assertEqual(source['previous_record_id'],source['next_record_id'])
