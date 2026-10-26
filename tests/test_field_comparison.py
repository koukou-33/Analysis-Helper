import csv
import json
import tempfile
import unittest
from pathlib import Path

import duckdb

from srs_helper.plans import Expression, expression_sql, QueryPlan, plan_fields, validate_plan_fields, PlanFieldError
from srs_helper.evidence import evaluate, feature_analysis
from srs_helper.pipeline import build_dataset
from srs_helper.knowledge import parse_document
from srs_helper.queries import Scope
from srs_helper.agent import DiagnosticEngine
from srs_helper.services import ModelService, ServiceConfig


class FieldComparisonTests(unittest.TestCase):
    def test_python_and_sql_boundaries_nulls_and_operators(self):
        with duckdb.connect() as con:
            for op in ['eq','ne','gt','ge','lt','le']:
                expr=Expression(op=op,field='measurement',value_field='threshold')
                for left,right in [(-5,-5),(2,1),(-1,1),(None,1),(1,None),(None,None)]:
                    params=[]
                    sql=expression_sql(expr,{'measurement','threshold'},params)
                    self.assertEqual(params,[])
                    actual=con.execute('SELECT '+sql+' FROM (SELECT ?::DOUBLE measurement, ?::DOUBLE threshold)',[left,right]).fetchone()[0]
                    self.assertEqual(actual,evaluate(expr,{'measurement':left,'threshold':right}))
                self.assertEqual(expression_sql(expr,{'measurement'},[]),'NULL::BOOLEAN')
                self.assertIsNone(evaluate(expr,{'measurement':1}))
                self.assertEqual(Expression.model_validate(expr.model_dump()),expr)

    def test_operand_contract_and_rhs_provenance(self):
        for data in [dict(op='le',field='a',value=1,value_field='b'),
                     dict(op='le',field='a',value_field=''),dict(op='is_null',field='a',value_field='b')]:
            with self.assertRaises(ValueError):
                Expression(**data)
        plan=QueryPlan(dataset_id='d',dataset_fingerprint='f',question='test',steps=[
            {'step_id':'s1','operator':'GROUP_AGG','metrics':[{'field':'threshold','aggregate':'avg','output':'mean_threshold'}]},
            {'step_id':'s2','operator':'FILTER','condition':{'op':'le','field':'measurement','value_field':'mean_threshold'}}])
        self.assertIn('mean_threshold',plan_fields(plan))
        with self.assertRaises(PlanFieldError) as caught:
            validate_plan_fields(plan,{'resource':['threshold','measurement'],'stream':[]})
        self.assertEqual(caught.exception.details['location'],'steps[1].condition.value_field')
        self.assertEqual(caught.exception.details['available_in'],['s1'])

    def test_spf_dynamic_threshold_activation_and_duplicate_conflict(self):
        document=parse_document(Path('knowledge_assets/features/SPF特性相关分析指南.md'),'feature')
        self.assertFalse(document['metadata'].get('unstructured',False))
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'NR_SRS.spf.csv'
            with source.open('w',newline='') as stream:
                writer=csv.writer(stream)
                writer.writerow(['ueLayerNum','uwTtiCnt1','logicCellId','crnti','uwData','uwTime',
                    'srsSpfSwitch','isAiranActInd','preSinr','spfPreSinrThd','spfPreSinrThd','codeUsrSpfSw','preSinrUeCurr','codeUsrSpfFlag'])
                cases=[(1,1,-5,-5,'',-5,1),(1,1,2,0,'',-1,0),(1,1,2,3,'',2,0),
                       (1,0,-5,-5,'',-5,0),(1,1,-5,'','',-5,0),(1,1,-5,-5,'',-5,2),
                       (1,1,-5,-5,-6,-5,0),(1,1,-5,-5,'',-4,0),(0,1,-5,'','',-5,0)]
                for i,(switch,active,history,threshold,duplicate,current,flag) in enumerate(cases):
                    writer.writerow([4,i,0,10,'2026-10-27','00:00:00.000',switch,active,history,threshold,duplicate,1,current,flag])
            directory,meta=build_dataset(source,root/'store',json.loads(Path('config/input_contract.json').read_text()))
            result=feature_analysis(directory,Scope(),meta,document['metadata'],root/'artifacts')
            self.assertEqual(result['activation_states'],{'True':3,'False':4,'None':2})
            self.assertEqual(result['flag_states'],{'True':1,'False':7,'None':1})
            self.assertEqual(result['expected_but_disabled'],1)
            self.assertTrue(result['complete_activation_rule'])
            self.assertEqual(result['conditions']['c2']['False'],1)
            self.assertEqual(result['conditions']['c3']['None'],3)
            assets=root/'assets'
            (assets/'features').mkdir(parents=True)
            (assets/'features'/'SPF特性相关分析指南.md').write_text(
                Path('knowledge_assets/features/SPF特性相关分析指南.md').read_text(encoding='utf-8'),encoding='utf-8')
            engine=DiagnosticEngine(root/'sessions',assets,model=ModelService(ServiceConfig()))
            response=engine.run(engine.store.create(directory),'分析 SPF')
            self.assertEqual(response['feature']['activation_states'],result['activation_states'])
            self.assertEqual(response['feature']['expected_but_disabled'],1)
            bad=dict(document['metadata'],activation_logic={'op':'eq','field':'c1','value_field':'spfPreSinrThd'})
            with self.assertRaisesRegex(ValueError,'evaluated condition IDs'):
                feature_analysis(directory,Scope(),meta,bad,root/'bad')
