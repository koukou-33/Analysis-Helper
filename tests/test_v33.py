import csv
import json
import tempfile
import unittest
from pathlib import Path

from srs_helper.field_metadata import compile_mapping
from srs_helper.services import parse_json_object
from srs_helper.explanation import validate_explanation, ExplanationError
from srs_helper.pipeline import build_dataset
from srs_helper.database import open_dataset
from srs_helper.queries import summarize
from srs_helper.queries import Scope
from srs_helper.peers import compare_peers
from srs_helper.agent import DiagnosticEngine
from srs_helper.services import ModelService, ServiceConfig
from srs_helper.cross_layer import describe_cross_layer
from srs_helper.case_assessment import assess_cases
from unittest.mock import patch
from uuid import uuid4


class V33Tests(unittest.TestCase):
    def test_descriptive_interval_and_case_criteria(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); directory,meta=self.make_peer_dataset(root)
            output=root/'out'; output.mkdir()
            artifacts=describe_cross_layer(directory,Scope(),'NI 趋势和跳频',['srsUeNiDb'],output)
            self.assertEqual(len(artifacts),2)
            self.assertTrue(all(r['rb_interval_count']==1 for r in artifacts[0]['preview']))
            self.assertTrue(all(r['trend_delta'] is None for r in artifacts[1]['preview']))
            doc={'path':'cases/sample.md','version':'v1','kind':'case','body':'NI 大于 -120',
                 'metadata':{'case_id':'sample','title':'sample','criteria':[{'condition_id':'c1','role':'NECESSARY',
                    'expression':{'op':'gt','field':'srsUeNiDb','value':-120},
                    'source':{'document_id':'cases/sample.md','revision':'v1','section':'条件','excerpt':'NI 大于 -120'}}]}}
            assessments,evidence=assess_cases(directory,Scope(),meta,[doc],output)
            self.assertEqual(assessments[0]['status'],'NECESSARY_CRITERIA_SUPPORTED')
            self.assertGreater(len(evidence),0)
            doc['metadata']['criteria'][0]['source']['excerpt']='invented'
            self.assertEqual(assess_cases(directory,Scope(),meta,[doc],output)[0][0]['status'],'INVALID_OR_UNSUPPORTED_CRITERIA')

    def make_peer_dataset(self, root):
        path = root/'NR_SRS.peers.csv'
        header = ['uwTtiCnt1','logicCellId','crnti','uwData','uwTime','ueLayerNum','usrRsrp','srsUeNiDb',
                  'freqRbStart','freqRbNum','subFrmNum','slot','ueSymPos']
        with path.open('w',newline='') as f:
            writer = csv.writer(f); writer.writerow(header)
            for n,(ue,ni,rb,symbol) in enumerate([(10,-110,0,2),(20,-100,0,2),(30,-90,24,2),(40,-95,0,3)]):
                writer.writerow([n,1,ue,'2026-10-26','00:00:00.000',4,-100,ni,rb,24,0,0,symbol])
        return build_dataset(path,root/'datasets',json.loads(Path('config/input_contract.json').read_text()))

    def test_peer_opt_in_exact_position_and_target_exclusion(self):
        self.assertEqual(compare_peers('missing',Scope(),[],'.')['status'],'NOT_REQUESTED')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); directory,_ = self.make_peer_dataset(root)
            result = compare_peers(directory,Scope(crnti=10),['srsUeNiDb'],root/'evidence',enabled=True)
            self.assertEqual(result['status'],'COMPLETED')
            row = result['artifacts'][0]['preview'][0]
            self.assertEqual((row['peer_ue_count'],row['peer_avg'],row['comparison_diff']),(1,-100,-10))
            self.assertEqual(compare_peers(directory,Scope(),['srsUeNiDb'],root/'evidence',enabled=True)['status'],'TARGET_REQUIRED')

    def test_ni_question_backfill_and_retry_without_requery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); directory,meta=self.make_peer_dataset(root)
            class Model:
                config=ServiceConfig(provider='local_openai',base_url='http://example',model='test')
                fail=True
                def chat_json(self,messages):
                    if self.fail:
                        raise RuntimeError('simulated interruption after query')
                    return {'claims':[],'hypotheses':[]}
            model=Model()
            engine=DiagnosticEngine(root/'sessions',root/'assets',model=model)
            sid=engine.store.create(directory); action=str(uuid4())
            plan={'dataset_id':meta['dataset_id'],'dataset_fingerprint':meta['dataset_fingerprint'],'question':'NI',
                  'steps':[{'step_id':'s1','operator':'GROUP_AGG','metrics':[{'field':'record_id','aggregate':'count','output':'n'}]}]}
            with self.assertRaises(RuntimeError):
                engine.run(sid,'分析 NI',action_id=action,plan_override=plan)
            model.fail=False
            with patch('srs_helper.agent.execute_plan',side_effect=AssertionError('completed queries must not repeat')):
                result=engine.run(sid,'分析 NI',action_id=action,plan_override=plan,retry=True)
            self.assertEqual(result['backfill_rounds'],1)
            self.assertEqual([(r['field'],r['status']) for r in result['requirements']],[('srsUeNiDb','COVERED')])
            self.assertFalse(any('解释未通过' in n for n in result['notices']))

    def test_output_envelopes_preserve_validation(self):
        self.assertEqual(parse_json_object('```json\n{"a":1}\n```'), {'a':1})
        for text in ['before\n```json\n{}\n```', '[]', '```json\n{bad}\n```']:
            with self.assertRaises(ValueError):
                parse_json_object(text)
        for prefix in ['artifact:', 'artifact：']:
            _, hypotheses = validate_explanation({'claims':[], 'hypotheses':[{'text':'check', 'references':[prefix+'abc']}]}, [{'artifact_id':'abc'}], [])
            self.assertEqual(hypotheses[0]['references'], ['abc'])
        with self.assertRaises(ExplanationError):
            validate_explanation({'claims':[], 'hypotheses':[{'text':'check','references':['artifact:ab']}]}, [{'artifact_id':'abc'}], [])

    def test_mapping_ranges_blanks_and_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'fields.md'
            p.write_text('|字段名称|含义|归属|\n|gain[0-3]|gain|L1测量|\n|gain[0]|||\n|gain[1]|different|L2|',encoding='utf-8')
            result = compile_mapping(p)
            self.assertEqual(len(result['fields']),4)
            self.assertEqual(result['fields']['gain[0]']['responsibility_layer'],'L1测量')
            self.assertEqual(result['fields']['gain[1]']['status'],'CONFLICT')
            self.assertIsNone(result['fields']['gain[1]']['responsibility_layer'])

    def test_layer_count_and_whole_record_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = root/'NR_SRS.test.csv'
            header = ['uwTtiCnt1','logicCellId','crnti','uwData','uwTime','ueLayerNum','usrRsrp','srsUeNiDb'] + [f'instBfSinrGain[{i}]' for i in range(4)]
            with p.open('w',newline='') as f:
                writer = csv.writer(f); writer.writerow(header)
                for n,layer in enumerate([1,2,4,8,0,'',-1,1.5,3,4]):
                    writer.writerow([n,1,10,'2026-10-26','00:00:00.000',layer,-50 if n==9 else -100,-110,4,3,2,1])
            directory, meta = build_dataset(p,root/'datasets',json.loads(Path('config/input_contract.json').read_text()))
            self.assertEqual(meta['rule_version'],'m1.5')
            with open_dataset(directory) as (con,_):
                self.assertEqual(con.execute('SELECT count(*) FROM l4_ue_resource_ts').fetchone()[0],4)
                self.assertEqual(con.execute('SELECT streamId,count(*) FROM l4_ue_stream_ts GROUP BY ALL ORDER BY 1').fetchall(),[(0,4),(1,3),(2,2),(3,2)])
                self.assertIsNone(con.execute('SELECT "instBfSinrGain[3]" FROM l4_ue_resource_ts WHERE ueLayerNum=1').fetchone()[0])
            summary = summarize(directory)
            self.assertEqual(summary['flow_valid_count'],10)
            self.assertEqual(summary['business_valid_count'],4)
            self.assertEqual(summary['ni_valid_count'],4)
            self.assertEqual(summary['quality']['invalid_layer_count'],5)
            self.assertEqual(summary['quality']['partial_stream_count'],1)
