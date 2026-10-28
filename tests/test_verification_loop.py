"""Counterexamples exercise real graph re-entry and actual additional DuckDB queries."""
import csv
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from srs_helper.agent import DiagnosticEngine
from srs_helper.services import ServiceConfig, ServiceError
from srs_helper.verification_loop import VerificationLoop, assess_counts, TaskProposal
from srs_helper.pipeline import build_dataset
import test_continuous_analysis as fixtures


class ScriptedModel:
    config=ServiceConfig(provider='local_openai',base_url='http://example',model='fake')
    def __init__(self, planner=None, explanation=None):
        self.planner=planner or (lambda p:{'tasks':[]})
        self.explanation=explanation
        self.planner_calls=0
        self.explain_calls=0
    def chat_json(self,messages):
        payload=json.loads(messages[1].content)
        if 'response_schema' in payload:
            self.planner_calls+=1
            return self.planner(payload)
        self.explain_calls+=1
        return self.explanation(payload) if self.explanation else {'claims':[],'hypotheses':[]}


def proposal(field='srsSpfSwitch',value=1,cohort='disabled',quantifier='ALL'):
    return {'goal_id':'disabled_conditions','cohort':cohort,
            'expression':{'op':'eq','field':field,'value':value},'quantifier':quantifier}


class VerificationLoopTests(unittest.TestCase):
    setUp=fixtures.ContinuousAnalysisTests.setUp
    tearDown=fixtures.ContinuousAnalysisTests.tearDown

    def engine_for(self,model):
        return DiagnosticEngine(self.root/str(uuid4()),self.root/'assets',model=model)

    def run_model(self,model,question='SPF 未生效原因'):
        engine=self.engine_for(model)
        return engine.run(engine.store.create(self.directory),question)

    def mutate(self, edit):
        path=self.root/'NR_SRS.states.csv'
        with path.open(newline='') as f:
            rows=list(csv.reader(f))
        edit(rows)
        with path.open('w',newline='') as f:
            csv.writer(f).writerows(rows)
        self.directory,self.meta=build_dataset(path,self.root/str(uuid4()),json.loads(Path('config/input_contract.json').read_text()))

    def test_dynamic_wrong_hypothesis_is_queried_refuted_then_followed(self):
        def plan(p):
            if not p['assessments']:
                return {'tasks':[proposal()]}
            if len(p['assessments'])==1:
                self.assertEqual(p['assessments'][0]['status'],'REFUTED')
                residual=p['assessments'][0]['residual_cohorts']['false']
                return {'tasks':[proposal('isAiranActInd',1,residual)]}
            return {'tasks':[]}
        from srs_helper.verification_loop import execute_plan
        with patch('srs_helper.verification_loop.execute_plan',wraps=execute_plan) as query:
            result=self.run_model(ScriptedModel(plan))
        self.assertEqual(query.call_count,2)
        self.assertEqual([a['status'] for a in result['verification_loop']['assessments']],['REFUTED','SUPPORTED'])
        self.assertEqual(result['steps'].count('loop_execute'),2)
        self.assertEqual(result['verification_loop']['stop_reason'],'GOALS_VERIFIED')
        self.assertIn('条件判断被反驳',result['answer'])
        self.assertTrue(all(a['references'] for a in result['hypotheses']))
        self.assertTrue(all(a['parent_artifact_ids'] for a in result['artifacts'] if a.get('origin')=='VERIFICATION_LOOP'))

    def test_final_explanation_reenters_queries_not_published_as_fact(self):
        model=ScriptedModel(explanation=lambda p:{'claims':[],'hypotheses':[{
            'text':'错误的自然语言结论，不能原样发布','references':[p['evidence'][0]['artifact_id']],
            'verification':proposal()}]})
        result=self.run_model(model)
        self.assertEqual(model.explain_calls,1)
        self.assertEqual(result['steps'].count('completion_gate'),2)
        self.assertEqual(result['verification_loop']['assessments'][0]['status'],'REFUTED')
        self.assertNotIn('错误的自然语言结论',result['answer'])
        self.assertNotIn('错误的自然语言结论',str(result['hypotheses']))

    def test_plain_rate_never_expands_into_cause_search(self):
        model=ScriptedModel(lambda p:(_ for _ in ()).throw(AssertionError('No planner for a rate')),
            lambda p:{'claims':[],'hypotheses':[{'text':'扩大原因猜测',
                    'references':[p['evidence'][0]['artifact_id']],'verification':proposal()}]})
        result=self.run_model(model,'SPF 生效比例')
        self.assertEqual(model.planner_calls,0)
        self.assertEqual(result['verification_loop']['round'],0)
        self.assertEqual(result['verification_loop']['stop_reason'],'GOALS_VERIFIED')
        self.assertEqual(result['hypotheses'],[])

    def test_repeated_equivalent_query_stops_without_claiming_no_data(self):
        model=ScriptedModel(lambda p:{'tasks':[proposal()]})
        result=self.run_model(model)
        self.assertEqual(result['verification_loop']['round'],1)
        self.assertEqual(result['verification_loop']['stop_reason'],'NO_PROGRESS')
        self.assertEqual(result['status'],'PARTIAL_FACTS_READY')
        self.assertEqual(result['verification_loop']['missing_information'],[])

    def test_service_failure_is_not_data_missing(self):
        def fail(p):
            raise ServiceError('empty content',code='CONTENT_EMPTY')
        result=self.run_model(ScriptedModel(fail))
        self.assertEqual(result['verification_loop']['stop_reason'],'TECHNICAL_FAILURE')
        self.assertEqual(result['verification_loop']['missing_information'],[])

    def test_query_failure_is_not_data_missing(self):
        with patch('srs_helper.verification_loop.execute_plan',side_effect=TimeoutError('query budget')):
            result=self.run_model(ScriptedModel(lambda p:{'tasks':[proposal()]}))
        self.assertEqual(result['verification_loop']['stop_reason'],'TECHNICAL_FAILURE')
        self.assertEqual(result['verification_loop']['validation_tasks'][0]['status'],'FAILED')

    def test_missing_fields_seed_real_queries_without_a_model(self):
        def edit(rows):
            for row in rows[1:]:
                if row[-1]=='0':
                    row[10]='0'
        self.mutate(edit)
        engine=self.engine_for(__import__('srs_helper.services',fromlist=['ModelService']).ModelService(ServiceConfig()))
        result=engine.run(engine.store.create(self.directory),'SPF 未生效原因')
        loop=result['verification_loop']
        self.assertEqual(loop['round'],2)
        self.assertEqual(loop['stop_reason'],'DATA_INSUFFICIENT')
        gaps={g['field']:g for g in loop['missing_information']}
        self.assertEqual(gaps['selLayerNum']['missing_type'],'FIELD_ABSENT')
        self.assertEqual(gaps['compFlag']['affected_count'],6)
        self.assertTrue(gaps['compFlag']['checked_artifacts'])

    def test_partial_unknown_keeps_true_false_unknown_subpopulations(self):
        result=self.run_model(ScriptedModel(lambda p:{'tasks':[proposal('codeUsrSpfFlag',1,'all')]}))
        assessment=result['verification_loop']['assessments'][0]
        self.assertEqual(assessment['counts'],{'total':12,'true':5,'false':6,'unknown':1})
        self.assertEqual(set(assessment['residual_cohorts']),{'true','false','unknown'})
        self.assertEqual(assessment['status'],'REFUTED')

    def test_unknown_field_outside_prompt_subset_is_still_queried(self):
        def plan(p):
            self.assertIn('codeUsrSpfSw',p['field_catalog'])
            return {'tasks':[]} if p['assessments'] else {'tasks':[proposal('codeUsrSpfSw',1)]}
        result=self.run_model(ScriptedModel(plan))
        self.assertEqual(result['verification_loop']['assessments'][0]['status'],'SUPPORTED')
        self.assertEqual(result['verification_loop']['missing_information'],[])

    def test_unconverted_model_prose_does_not_prove_business_criteria_missing(self):
        model=ScriptedModel(explanation=lambda p:{'claims':[],'hypotheses':[{
            'text':'互斥可能导致关闭','references':[p['evidence'][0]['artifact_id']]}]})
        result=self.run_model(model)
        self.assertEqual(result['verification_loop']['stop_reason'],'NO_PROGRESS')
        self.assertEqual(result['hypotheses'],[])
        self.assertEqual(result['verification_loop']['missing_information'],[])

    def test_scope_or_unit_expansion_rejected(self):
        for extra in ({'analysis_unit':'session'},{'cohort':'some_other_users'},{'goal_id':'user_reason_ratio'}):
            p={**proposal(),**extra}
            result=self.run_model(ScriptedModel(lambda payload:{'tasks':[p]}))
            self.assertEqual(result['verification_loop']['round'],0)
            self.assertEqual(result['verification_loop']['stop_reason'],'TECHNICAL_FAILURE')

    def test_interruption_after_query_resumes_evaluation_without_requery(self):
        model=ScriptedModel(lambda p:{'tasks':[]} if p['assessments'] else {'tasks':[proposal()]})
        engine=self.engine_for(model); sid=engine.store.create(self.directory); action=str(uuid4())
        with patch.object(VerificationLoop,'evaluate',side_effect=RuntimeError('interrupt after persisted query')):
            with self.assertRaises(RuntimeError):
                engine.run(sid,'SPF 未生效原因',action_id=action)
        with patch('srs_helper.verification_loop.execute_plan',side_effect=AssertionError('No repeat query')):
            result=engine.run(sid,'SPF 未生效原因',action_id=action,retry=True)
        self.assertEqual(result['verification_loop']['round'],1)
        self.assertEqual(len(result['verification_loop']['query_ledger']),1)
        self.assertEqual(result['verification_loop']['assessments'][0]['status'],'REFUTED')

    def test_budget_exhaustion_keeps_pending_work_and_never_claims_missing_data(self):
        original=VerificationLoop.__init__
        def limited(loop,**kwargs):
            original(loop,**kwargs)
            loop.remaining=lambda:0
        with patch.object(VerificationLoop,'__init__',limited):
            result=self.run_model(ScriptedModel(lambda p:{'tasks':[proposal()]}))
        self.assertEqual(result['verification_loop']['stop_reason'],'BUDGET_EXHAUSTED')
        self.assertEqual(result['verification_loop']['round'],0)
        self.assertEqual(result['verification_loop']['missing_information'],[])

    def test_all_null_and_invalid_values_have_distinct_inventory_evidence(self):
        def edit(rows):
            rows[0].extend(['all_null_field','invalid_field'])
            for row in rows[1:]:
                row.extend(['','not_a_number'])
        self.mutate(edit)
        model=ScriptedModel(lambda p:{'tasks':[]} if len(p['assessments'])==2 else
            {'tasks':[proposal('all_null_field',0),proposal('invalid_field',0)]})
        result=self.run_model(model)
        self.assertEqual(result['verification_loop']['stop_reason'],'DATA_INSUFFICIENT')
        gaps={g['field']:g for g in result['verification_loop']['missing_information']}
        self.assertEqual(gaps['all_null_field']['missing_type'],'ALL_NULL')
        self.assertEqual(gaps['invalid_field']['missing_type'],'INVALID_VALUES')
        self.assertEqual(gaps['invalid_field']['affected_count'],6)
        self.assertTrue(any(a['operator']=='LOOP_FIELD_INVENTORY' for a in result['artifacts']))

    def test_empty_cohort_is_not_vacuous_support_or_global_missing_data(self):
        first=self.engine.run(self.sid,'SPF 生效比例')
        model=ScriptedModel(lambda p:{'tasks':[]} if p['assessments'] else {'tasks':[proposal(cohort='enabled')]})
        self.engine.model=model
        result=self.engine.run(self.sid,'SPF 未生效原因',parent_action_id=first['action_id'],cohort_name='disabled')
        self.assertEqual(result['verification_loop']['assessments'][0]['counts']['total'],0)
        self.assertEqual(result['verification_loop']['assessments'][0]['status'],'INCONCLUSIVE')
        self.assertEqual(result['verification_loop']['stop_reason'],'NO_PROGRESS')
        self.assertEqual(result['verification_loop']['missing_information'],[])

    def test_known_unmet_condition_does_not_require_irrelevant_null_condition(self):
        def edit(rows):
            for row in rows[1:]:
                row[8]=''
        self.mutate(edit)
        result=self.run_model(ScriptedModel())
        self.assertEqual(result['verification_loop']['round'],0)
        self.assertEqual(result['verification_loop']['stop_reason'],'GOALS_VERIFIED')

    def test_registered_investigation_preserves_unexplained_residual(self):
        def edit(rows):
            rows[0].extend(['selLayerNum','compFlag'])
            for row in rows[1:]:
                if row[-1]=='0':
                    row[10]='0'
                row.extend(['1','0'])
        self.mutate(edit)
        result=self.run_model(ScriptedModel())
        self.assertEqual(result['verification_loop']['round'],2)
        self.assertEqual([a['status'] for a in result['verification_loop']['assessments']],['REFUTED','REFUTED'])
        gap=next(g for g in result['verification_loop']['missing_information'] if g['cohort']=='unexplained_code_user_switch')
        self.assertEqual(gap['affected_count'],6)
        self.assertEqual(gap['missing_type'],'CRITERION_UNDEFINED')
        self.assertEqual(result['verification_loop']['stop_reason'],'CRITERIA_REQUIRED')

    def test_same_query_different_quantifier_reuses_evidence(self):
        def plan(p):
            if len(p['assessments'])==0:
                return {'tasks':[proposal('codeUsrSpfFlag',1,'all','ALL')]}
            if len(p['assessments'])==1:
                return {'tasks':[proposal('codeUsrSpfFlag',1,'all','ANY')]}
            return {'tasks':[]}
        result=self.run_model(ScriptedModel(plan))
        loop=result['verification_loop']
        self.assertEqual(loop['round'],1)
        self.assertEqual([a['status'] for a in loop['assessments']],['REFUTED','SUPPORTED'])
        self.assertEqual(loop['assessments'][0]['evidence_refs'],loop['assessments'][1]['evidence_refs'])

    def test_invalid_task_has_one_budgeted_repair_before_execution(self):
        def plan(p):
            if p['assessments']:
                return {'tasks':[]}
            if 'validation_feedback' not in p:
                return {'tasks':[proposal(cohort='invalid_population')]}
            self.assertIn('Unknown population',str(p['validation_feedback']))
            return {'tasks':[proposal()]}
        result=self.run_model(ScriptedModel(plan))
        self.assertEqual(result['verification_loop']['round'],1)
        self.assertEqual(result['verification_loop']['planner_calls'],3)
        self.assertEqual(result['verification_loop']['assessments'][0]['status'],'REFUTED')

    def test_quantifier_unknown_and_empty_semantics(self):
        self.assertEqual(assess_counts('ALL',0,0,0),'INCONCLUSIVE')
        self.assertEqual(assess_counts('ALL',2,0,1),'INCONCLUSIVE')
        self.assertEqual(assess_counts('ALL',2,1,1),'REFUTED')
        self.assertEqual(assess_counts('ANY',2,1,1),'SUPPORTED')
        self.assertEqual(assess_counts('ANY',0,2,1),'INCONCLUSIVE')
