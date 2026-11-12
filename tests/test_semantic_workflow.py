import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from srs_helper.agent import DiagnosticEngine
from srs_helper.pipeline import build_dataset
from srs_helper.question_scope import resolve_question, confirm_request, normalize_scope
from srs_helper.queries import Scope
from srs_helper.services import ServiceConfig
from srs_helper.task_review import review_tasks
from srs_helper.expert_tools import run_tools


class SemanticWorkflowTests(unittest.TestCase):
    def test_reopened_review_cannot_silently_exhaust_budget(self):
        ledger=run_tools(propose=lambda _:self.fail('No remaining budget'),payload=lambda:{},
            query=lambda _:None,search=lambda _:None,save=lambda _:None,
            restored=[{'status':'FINISHED'},{'status':'REOPENED'}],remaining=lambda:0)
        self.assertEqual(ledger[-1]['error'],'BUDGET_EXHAUSTED')

    def test_unknown_task_evidence_cannot_complete(self):
        intent={'tasks':[{'task_id':'goal_1','objective':'比较两个小区','required':True}]}
        review=review_tasks(intent,[],{'status':'PASS'},lambda _: {'assessments':[
            {'task_id':'goal_1','status':'SUPPORTED','evidence_ids':['invented'],'reason':'已完成'}]})
        self.assertEqual(review['status'],'LIMITED')
        self.assertEqual(review['error'],'ValueError')

    def test_data_gap_overrides_semantic_self_assessment(self):
        review=review_tasks({'tasks':[{'task_id':'goal_1','objective':'趋势'}]},
            [{'artifact_id':'a'}],{'status':'LIMITED'},lambda _: {'assessments':[
                {'task_id':'goal_1','status':'SUPPORTED','evidence_ids':['a'],'reason':'已有统计'}]})
        self.assertEqual(review['tasks'][0]['status'],'LIMITED')

    def test_joint_request_and_freeze_gate(self):
        class Model:
            config=ServiceConfig(provider='local_openai',base_url='http://example',model='test')
            def chat_json(self,messages):
                system=messages[0].content
                if '理解用户的专家分析任务' in system:
                    return {'goals':['联合查看两个小区的TA变化'],'metrics':['taValue'],
                        'selection_mode':'EXPLICIT','logic_cell_ids':[1,2],
                        'selection_quote':'小区1和2','same_subject':True,
                        'same_subject_quote':'小区1和2是同一个用户','trend':True,'view':'JOINT'}
                if '分析工具调度器' in system:
                    return {'action':'finish','purpose':'当前问题需要的两小区原始趋势已取得'}
                if '分析任务覆盖审核器' in system:
                    payload=json.loads(messages[1].content)
                    ids=[a['artifact_id'] for a in payload['evidence']['artifacts']]
                    return {'assessments':[{'task_id':t['task_id'],'status':'SUPPORTED',
                        'evidence_ids':ids,'reason':'两个小区均有精细观测，分别展示原始值。'} for t in payload['tasks']]}
                return {'claims':[],'hypotheses':[]}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'NR_SRS.csv'
            with source.open('w',newline='',encoding='utf-8') as stream:
                writer=csv.writer(stream)
                writer.writerow(['uwTtiCnt1','logicCellId','crnti','uwData','uwTime','ueLayerNum','frmNum','subFrmNum','slot','resourceId','taValue'])
                for cell in (1,2):
                    for frame in range(3):
                        writer.writerow([100+frame*10,cell,1000,'2026-11-13','10:11:21',4,frame,0,0,0,
                                         [18,74,20][frame] if cell==1 else 16])
            contract=json.loads(Path('config/input_contract.json').read_text())
            contract['time_format']='%H:%M:%S'
            directory,_=build_dataset(source,root/'data',contract)
            model=Model(); question='小区1和2是同一个用户，放在一起看TA变化'
            candidate=resolve_question(question,Scope(),directory,model=model)
            self.assertFalse(candidate['issues'])
            scope=normalize_scope(candidate['scope'])
            request=confirm_request(directory,question,scope,['taValue'],True,single_subject=True,
                intent_spec=candidate['intent_spec'],execution_mode='AUTO_FROM_QUESTION')
            engine=DiagnosticEngine(root/'store',root/'assets',model=model)
            result=engine.run(engine.store.create(directory),question,scope,confirmed_request=request)
            self.assertEqual(result['data_audit']['status'],'PASS')
            self.assertEqual(result['task_review']['status'],'PASS')
            self.assertEqual(result['conclusion_audit']['status'],'PASSED')
            artifact=next(a for a in result['artifacts'] if a['operator']=='METRIC_TIME_SERIES')
            self.assertEqual(artifact['statistics']['points'],6)
            self.assertEqual({r['logicCellId'] for r in artifact['group_statistics']},{1,2})
            self.assertEqual(result['steps'],['plan','agent_loop','explain','report'])
            self.assertIn('data_review',result['analysis_steps'])
            self.assertEqual(result['report_index']['task_review']['status'],'PASS')


if __name__=='__main__':
    unittest.main()
