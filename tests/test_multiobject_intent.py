import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import duckdb

from srs_helper.pipeline import build_dataset
from srs_helper.queries import Scope
from srs_helper.question_scope import (parse_question, resolve_question, normalize_scope,
    confirm_request, validate_request, scope_dict, identity)


class Model:
    config=SimpleNamespace(configured=True)
    def __init__(self, responses): self.responses=list(responses); self.calls=[]
    def chat_json(self,messages):
        self.calls.append(messages)
        response=self.responses.pop(0)
        if isinstance(response,Exception): raise response
        return response


class MultiObjectIntentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(); cls.root=Path(cls.tmp.name)
        path=cls.root/'NR_SRS.csv'
        with path.open('w',newline='',encoding='utf-8') as stream:
            w=csv.writer(stream)
            w.writerow(['ueLayerNum','uwTtiCnt1','logicCellId','crnti','uwData','uwTime','taValue','customMetric'])
            for i,(cell,ue) in enumerate([(1,100),(1,200),(2,100),(2,200)]):
                w.writerow([4,i,cell,ue,'2026-11-13',f'00:00:0{i}.000',10+i,100+i])
        contract=json.loads(Path('config/input_contract.json').read_text())
        cls.directory,cls.meta=build_dataset(path,cls.root/'data',contract)

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def test_clear_cell_lists_and_hard_scope_conflicts(self):
        for q in ['小区1和2是同一个用户，放在一起看TA变化','小区1和小区2是同一个用户，TA变化如何','先看小区1再对比小区2的TA变化']:
            c=parse_question(q,Scope())
            self.assertEqual(c['scope']['logic_cell_ids'],[1,2])
            self.assertIsNone(c['scope']['logic_cell_id'])
            self.assertFalse(c['issues'])
        c=parse_question('这份数据两个小区是同一个用户，TA变化',Scope(logic_cell_id=1))
        self.assertTrue(c['issues'])
        self.assertEqual(c['scope']['logic_cell_id'],1)
        self.assertEqual(parse_question('不要小区1，只看小区2的TA',Scope())['scope']['logic_cell_id'],2)

    def test_paired_predicates_do_not_form_cartesian_product(self):
        q='比较小区1的CRNTI100与小区2的CRNTI200的TA变化'
        candidate=parse_question(q,Scope())
        scope=normalize_scope(candidate['scope'])
        where,params=scope.where()
        con=duckdb.connect()
        rows=con.execute('SELECT * FROM (VALUES (1,100),(1,200),(2,100),(2,200)) t(logicCellId,crnti) WHERE '+where,params).fetchall()
        con.close()
        self.assertEqual(rows,[(1,100),(2,200)])
        request=confirm_request(self.directory,q,scope,['taValue'],True)
        self.assertEqual(request['preflight']['records'],2)
        self.assertEqual(request['preflight']['cells'],2)
        validate_request(json.loads(json.dumps(request)),self.meta,q,scope)

    def test_legacy_scope_hash_and_request_compatibility(self):
        old={'logic_cell_id':None,'ue_uid':None,'crnti':None,'resource_id':None,'start':None,'end':None,'phy_cell_inst':None}
        self.assertEqual(Scope().__dict__,old)
        self.assertEqual(identity(Scope().__dict__),identity(old))
        self.assertEqual(scope_dict(Scope(logic_cell_ids=None,subjects=None)),old)
        request=confirm_request(self.directory,'TA',Scope(),['taValue'],False)
        validate_request(request,self.meta,'TA',Scope())
        with self.assertRaises(ValueError): validate_request(request,self.meta,'TA',Scope(logic_cell_ids=[1]))

    def test_untrusted_scope_fields_and_values_rejected(self):
        with self.assertRaises(ValueError): Scope(subjects=[{'sql':'1=1'}])
        with self.assertRaises(ValueError): Scope(logic_cell_ids=[True])
        with self.assertRaises(ValueError): Scope(subjects=[{}])
        with self.assertRaises(ValueError): Scope(subjects=[{'crnti':'100 OR 1=1'}]).where()
        with self.assertRaises(ValueError): Scope(logic_cell_id=1,logic_cell_ids=[2]).where()
        with self.assertRaises(ValueError): Scope(logic_cell_id=1,logic_cell_ids=[1,2]).where()

    def test_model_semantics_are_bound_to_all_requested_objects(self):
        q='比较小区1和2的customMetric随时间的表现'
        response={'goals':['比较两个小区的指标变化'],'metrics':['customMetric'],'selection_mode':'EXPLICIT',
            'logic_cell_ids':[1,2],'selection_quote':'小区1和2','trend':True,'view':'JOINT'}
        model=Model([response])
        c=resolve_question(q,Scope(),self.directory,model=model)
        self.assertEqual(c['intent_status'],'MODEL_VALIDATED')
        self.assertEqual(c['intent_spec']['expected_cells'],[1,2])
        self.assertEqual(c['intent_spec']['view']['mode'],'JOINT')
        self.assertEqual([m['canonical_field'] for m in c['metrics']],['customMetric'])
        request=confirm_request(self.directory,q,normalize_scope(c['scope']),['customMetric'],True,intent_spec=c['intent_spec'])
        self.assertEqual(request['preflight']['records'],4)
        self.assertTrue(request['intent_spec']['tasks'])
        bad={**response,'logic_cell_ids':[1]}
        failed=resolve_question(q,Scope(),self.directory,model=Model([bad,bad]))
        self.assertEqual(failed['intent_status'],'DETERMINISTIC_FALLBACK')
        self.assertEqual(failed['scope']['logic_cell_ids'],[1,2])
        self.assertEqual(len(failed['intent_diagnostics']),2)

    def test_ambiguous_same_subject_and_model_field_invention(self):
        q='这两个小区是同一个用户，放在一起看TA变化'
        c=resolve_question(q,Scope(),self.directory)
        self.assertEqual(c['scope']['logic_cell_ids'],[1,2])
        self.assertTrue(any('CRNTI' in issue for issue in c['issues']))
        with self.assertRaises(ValueError): confirm_request(self.directory,q,Scope(),['taValue'],True,True,'AUTO_FROM_QUESTION')
        response={'goals':['查看变化'],'metrics':['nonexistent'],'trend':True}
        c=resolve_question('TA变化',Scope(),self.directory,model=Model([response,response]))
        self.assertEqual(c['intent_status'],'DETERMINISTIC_FALLBACK')
        self.assertNotIn('nonexistent',[m['canonical_field'] for m in c['metrics']])
        self.assertFalse(parse_question('两个小区是否是同一个用户，TA变化如何',Scope())['single_subject_statement'])

    def test_explicit_paired_subject_membership_is_persisted(self):
        q='小区1的CRNTI100与小区2的CRNTI200是同一个用户，放在一起看TA变化'
        c=resolve_question(q,Scope(),self.directory)
        self.assertFalse(c['issues'])
        request=confirm_request(self.directory,q,normalize_scope(c['scope']),['taValue'],True,
            single_subject=True,execution_mode='AUTO_FROM_QUESTION',intent_spec=c['intent_spec'])
        self.assertEqual({(m['logic_cell_id'],m['crnti']) for m in request['subject_assertion']['members']},{(1,100),(2,200)})
        self.assertEqual(request['intent_spec']['subject_basis'],'USER_STATEMENT')


if __name__=='__main__': unittest.main()
