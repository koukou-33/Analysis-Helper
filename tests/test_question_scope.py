import csv
import json
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from srs_helper.question_scope import parse_question, confirm_request, validate_request
from srs_helper.queries import Scope
from srs_helper.pipeline import build_dataset
from srs_helper.agent import DiagnosticEngine
from srs_helper.services import ModelService,ServiceConfig

class QuestionScopeTests(unittest.TestCase):
    def test_candidates_and_conflicts(self):
        candidate=parse_question('1911这个用户的TA变化',Scope())
        self.assertEqual(candidate['scope']['crnti'],1911)
        self.assertEqual(candidate['metrics'][0]['canonical_field'],'taValue')
        self.assertTrue(parse_question('1911这个用户',Scope(crnti=3))['issues'])
        self.assertTrue(parse_question('1911和1912',Scope())['issues'])
        self.assertEqual(parse_question('CRNTI=0x777',Scope())['scope']['crnti'],1911)
        self.assertTrue(parse_question('昨天TA',Scope())['issues'])
        self.assertTrue(parse_question('所有crnti实际都是一个用户，他的TA变化',Scope())['single_subject_statement'])
        self.assertFalse(parse_question('所有crnti是不是一个用户，TA变化如何',Scope())['single_subject_statement'])

    def test_confirmed_trend_full_scope_and_integrity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'NR_SRS.csv'
            with source.open('w',newline='') as stream:
                w=csv.writer(stream); w.writerow(['ueLayerNum','uwTtiCnt1','logicCellId','crnti','uwData','uwTime','taValue'])
                for i,(ue,v) in enumerate([(1911,1),(1911,''),(1911,3),(1911,4),(22,99)]):
                    w.writerow([4,i,0 if ue==1911 else 1,ue,'2026-11-05',f'00:00:0{i}.000',v])
            directory,meta=build_dataset(source,root/'data',json.loads(Path('config/input_contract.json').read_text()))
            scope=Scope(crnti=1911)
            request=confirm_request(directory,'1911这个用户的TA变化',scope,['taValue'],True)
            self.assertEqual(request['preflight']['records'],4)
            with self.assertRaises(ValueError): validate_request(request,meta,request['question'],Scope())
            from unittest.mock import patch
            with patch('srs_helper.question_scope.mapping',return_value=({},'new_mapping')):
                with self.assertRaises(ValueError): validate_request(request,meta,request['question'],scope)
                validate_request(request,meta,request['question'],scope,check_mapping=False)
            empty=confirm_request(directory,'不存在的用户',Scope(crnti=77),['taValue'],True)
            self.assertEqual(empty['preflight']['records'],0)
            wrong={**request,'trend':False}
            with self.assertRaises(ValueError): validate_request(wrong,meta,request['question'],scope)
            engine=DiagnosticEngine(root/'store',root/'assets',model=ModelService(ServiceConfig()))
            sid=engine.store.create(directory)
            result=engine.run(sid,request['question'],scope,confirmed_request=request)
            self.assertEqual(result['scope']['crnti'],1911)
            artifact=next(a for a in result['artifacts'] if a['operator']=='METRIC_TIME_SERIES')
            self.assertEqual(artifact['statistics']['maximum'],4)
            self.assertEqual(artifact['statistics']['valid_points'],3)
            self.assertEqual(result['confirmed_request']['request_hash'],request['request_hash'])
            self.assertIn('taValue',result['answer'])
            import pyarrow.parquet as pq
            points=pq.read_table(artifact['path']).to_pylist()
            self.assertIsNone(points[1]['value'])
            self.assertNotEqual(points[0]['segment'],points[2]['segment'])
            self.assertEqual(artifact['group_statistics'][0]['first_value'],1)
            self.assertEqual(artifact['group_statistics'][0]['last_value'],4)
            self.assertEqual(artifact['confirmed_request_hash'],request['request_hash'])
            with self.assertRaises(ValueError):
                confirm_request(directory,'bad',Scope(crnti=-1),['taValue'],True)
            with self.assertRaises(ValueError):
                confirm_request(directory,'bad',scope,['not_registered'],True)
            unavailable=confirm_request(directory,'缺失',scope,[],False)
            self.assertEqual(unavailable['metrics'],[])
            all_question='所有crnti实际都是一个用户，不同接入后不同的crnti，他在不同小区的TA变化怎么样'
            all_request=confirm_request(directory,all_question,Scope(),['taValue'],True,single_subject=True,execution_mode='AUTO_FROM_QUESTION')
            self.assertEqual(all_request['subject_assertion']['source'],'EXPLICIT_USER_STATEMENT')
            from srs_helper.question_scope import query_logic
            self.assertIn('当前数据集全部范围',query_logic(all_request))
            self.assertIn('仅使用所选父证据群体',query_logic(all_request,inherited=True))
            all_result=engine.run(sid,all_question,Scope(),confirmed_request=all_request)
            self.assertIn('依据用户本次说明',all_result['answer'])
            all_artifact=next(a for a in all_result['artifacts'] if a['operator']=='METRIC_TIME_SERIES')
            all_points=pq.read_table(all_artifact['path']).to_pylist()
            self.assertEqual({v for row in all_points for v in row['crntis']},{1911,22})
            self.assertEqual(all_artifact['statistics']['maximum'],99)
            self.assertEqual({r['logicCellId'] for r in all_points},{0,1})
            self.assertEqual(all_request['subject_assertion']['scope'],all_result['scope'])
            self.assertIsNone(confirm_request(directory,all_question,Scope(),['taValue'],True)['subject_assertion'])
