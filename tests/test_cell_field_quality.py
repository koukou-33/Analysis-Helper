"""Field-local cell inference must not erase or spread unrelated parse failures."""
import csv
import json
import tempfile
import unittest
from pathlib import Path

import pyarrow.parquet as pq

from srs_helper.database import open_dataset
from srs_helper.pipeline import build_dataset
from srs_helper.plans import Expression, QueryPlan, Step, execute_plan
from srs_helper.provenance import evidence_contains_record, record_trace
from srs_helper.queries import Scope


class CellFieldQualityTests(unittest.TestCase):
    header=['ueLayerNum','uwTtiCnt1','logicCellId','crnti','uwData','uwTime',
            'resourceId','usrRsrp','srsSpfSwitch']

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def row(self,second,**changes):
        return dict({'ueLayerNum':4,'uwTtiCnt1':second,'logicCellId':0,'crnti':10,
                     'uwData':'2026-10-30','uwTime':f'00:00:{second:02d}.000',
                     'resourceId':1,'usrRsrp':-100,'srsSpfSwitch':''},**changes)

    def build(self,rows,header=None,contract_updates=None):
        header=header or self.header
        source=self.root/'NR_SRS.csv'
        with source.open('w',newline='',encoding='utf-8') as stream:
            writer=csv.writer(stream)
            writer.writerow(header)
            writer.writerows([r.get(name,'') for name in header] if isinstance(r,dict) else r for r in rows)
        contract=json.loads(Path('config/input_contract.json').read_text(encoding='utf-8'))
        contract.update(contract_updates or {})
        return build_dataset(source,self.root/'data',contract)

    def query(self,directory,meta,field='srsSpfSwitch',value=1):
        plan=QueryPlan(dataset_id=meta['dataset_id'],dataset_fingerprint=meta['dataset_fingerprint'],
            question='字段级补齐核验',steps=[Step(step_id='s1',operator='CONDITION_COUNT',
                conditions={'c0':Expression(op='eq',field=field,value=value)})])
        artifacts=execute_plan(directory,Scope(),plan,self.root/'artifacts')
        return pq.read_table(artifacts[0]['path']).to_pylist(),artifacts

    def test_unrelated_parse_failure_allows_donors_and_recipient_with_lineage(self):
        rows=[self.row(i,crnti=10+i,resourceId='bad',srsSpfSwitch=value)
              for i,value in enumerate([1,'',1])]
        directory,meta=self.build(rows)
        result,artifacts=self.query(directory,meta)
        middle=next(r for r in result if r['file_row_id']==2)
        self.assertEqual(middle['srsSpfSwitch'],1)
        self.assertTrue(all(r['has_parse_error'] for r in result))
        self.assertIsNone(middle['__cell_raw_srsSpfSwitch'])
        provenance=middle['__cell_fill_srsSpfSwitch']
        for key,expected_row in [('previous_record_id',1),('next_record_id',3)]:
            self.assertEqual(record_trace(directory,provenance[key])['file_row_id'],expected_row)
            self.assertTrue(evidence_contains_record(artifacts[0],provenance[key]))
        self.assertEqual(artifacts[0]['imputed_record_count'],1)
        self.assertEqual(meta['contract']['cell_parameter_policy']['version'],'cell-continuity.v2')
        with open_dataset(directory) as (con,_):
            self.assertEqual(con.execute('SELECT count(*) FROM l2_standardized WHERE has_parse_error').fetchone()[0],3)
            self.assertEqual(con.execute("SELECT count(*) FROM l2_extra_numeric WHERE field_name='srsSpfSwitch'").fetchone()[0],2)

    def test_target_invalid_nonfinite_and_duplicate_conflict_are_boundaries(self):
        header=self.header+['srsSpfSwitch']
        rows=[]
        for cell,invalid in enumerate(['bad','nan',2,'conflict']):
            for second,value in enumerate([1,'',invalid,'',1]):
                row=self.row(second,logicCellId=cell,srsSpfSwitch=value)
                raw=[row.get(name,'') for name in header]
                if invalid=='conflict' and second==2:
                    raw[header.index('srsSpfSwitch')]=0
                    raw[-1]=1
                rows.append(raw)
        directory,meta=self.build(rows,header)
        _,artifacts=self.query(directory,meta)
        self.assertEqual(artifacts[0]['imputed_record_count'],0)
        with open_dataset(directory) as (con,_):
            self.assertEqual(con.execute('SELECT count(*) FROM cell_parameter_fills').fetchone()[0],0)

    def test_core_target_alias_errors_are_not_treated_as_missing_values(self):
        semantics=self.root/'fields.md'
        semantics.write_text('| 字段名称 | 含义 | 归属 | 数据层级 |\n| usrRsrp | 测试参数 | L1测量 | 小区级 |\n',encoding='utf-8')
        aliases=self.root/'aliases.json'
        aliases.write_text(json.dumps({'aliases':{'usrRsrp':'rawRsrp'}}),encoding='utf-8')
        header=[name if name!='usrRsrp' else 'rawRsrp' for name in self.header]+['rawRsrp']
        rows=[]
        for cell,invalid in enumerate(['bad','nan','conflict']):
            for second,value in enumerate([-100,'',invalid,'',-100]):
                row=self.row(second,logicCellId=cell,rawRsrp=value)
                raw=[row.get(name,'') for name in header]
                if invalid=='conflict' and second==2:
                    raw[header.index('rawRsrp')]=-100
                    raw[-1]=-101
                rows.append(raw)
        rows.extend(self.row(i,logicCellId=9,rawRsrp=value) for i,value in enumerate([-100,'',-100]))
        directory,meta=self.build(rows,header,{'field_semantics_file':str(semantics),'field_mapping_file':str(aliases)})
        result,artifacts=self.query(directory,meta,'usrRsrp',-100)
        self.assertEqual(artifacts[0]['imputed_record_count'],1)
        for cell in range(3):
            group=[r for r in result if r['logicCellId']==cell]
            self.assertTrue(all(r['__cell_fill_usrRsrp'] is None for r in group))
            malformed=next(r for r in group if str(r['event_ts']).endswith('00:00:02'))
            self.assertIsNone(malformed['usrRsrp'])
            self.assertTrue(malformed['has_parse_error'])
        self.assertTrue(all(r['usrRsrp']==-100 for r in result if r['logicCellId']==9))

    def test_short_and_long_csv_rows_block_inference_across_them(self):
        header=self.header+['unusedTail']
        rows=[]
        for cell in (0,1):
            for second,value in enumerate([1,'',1,'',1]):
                row=self.row(second,logicCellId=cell,srsSpfSwitch=value,unusedTail='')
                raw=[row.get(name,'') for name in header]
                if second==2:
                    raw=raw[:-1] if cell==0 else raw+['unexpected']
                rows.append(raw)
        directory,meta=self.build(rows,header)
        _,artifacts=self.query(directory,meta)
        self.assertEqual(artifacts[0]['imputed_record_count'],0)
        with open_dataset(directory) as (con,_):
            self.assertEqual(con.execute('SELECT count(*) FROM l4_ue_resource_ts WHERE NOT csv_structure_complete').fetchone()[0],2)

    def test_missing_identity_or_time_does_not_supply_cell_anchors(self):
        directory,meta=self.build([
            self.row(0,srsSpfSwitch=1),
            self.row(1,srsSpfSwitch=1,logicCellId='bad'),
            self.row(2),
            self.row(3,srsSpfSwitch=1,uwTime='bad'),
        ])
        _,artifacts=self.query(directory,meta)
        self.assertEqual(artifacts[0]['imputed_record_count'],0)

    def test_invalid_layers_and_suspicious_records_remain_outside_business_scope(self):
        directory,meta=self.build([
            self.row(0,srsSpfSwitch=1),
            self.row(1,ueLayerNum=0),
            self.row(2,usrRsrp=-200),
            self.row(3),
            self.row(4,srsSpfSwitch=1),
        ])
        result,artifacts=self.query(directory,meta)
        self.assertEqual({r['file_row_id'] for r in result},{1,4,5})
        self.assertEqual(artifacts[0]['imputed_record_count'],1)
        with open_dataset(directory) as (con,_):
            self.assertEqual(con.execute('SELECT count(*) FROM l3_cleaned_srs WHERE NOT business_eligible').fetchone()[0],2)
            self.assertEqual(con.execute('''SELECT count(*) FROM cell_parameter_fills f
                JOIN l3_cleaned_srs r USING(record_id) WHERE NOT r.business_eligible''').fetchone()[0],0)
