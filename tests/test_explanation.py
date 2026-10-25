import unittest
from unittest.mock import patch

from srs_helper.explanation import ExplanationError, validate_explanation
from srs_helper.plans import QueryPlan, Step


class ExplanationTests(unittest.TestCase):
    def test_operator_semantics_fail_before_execution(self):
        for step in [
            {'operator':'CONDITION_COUNT','condition':{'op':'eq','field':'gain_missing','value':True}},
            {'operator':'CONDITION_COUNT','conditions':{'bad':{'op':'is_null','field':'x'}}},
            {'operator':'GROUP_AGG'}, {'operator':'FILTER'}, {'operator':'COMPARE'},
            {'operator':'TIME_SLICE','start':'2026-10-25','end':'2026-10-24'},
            {'operator':'TEMPORAL_RELATE','other_source':'resource','object_keys':['crnti']}]:
            with self.subTest(step=step),self.assertRaises(ValueError):
                QueryPlan(dataset_id='a',dataset_fingerprint='b',question='test',steps=[{'step_id':'s1',**step}])
        self.assertEqual(Step(step_id='s1',operator='CONDITION_COUNT',conditions={'c1':{'op':'is_null','field':'x'}}).operator,'CONDITION_COUNT')

    def test_explanation_error_locations(self):
        base = {'artifact_id':'a','column':'gain_avg','row_key':{'streamId':0},'value':1.5,'unit':'dB'}
        for update,code,location in [
            ({'unit':'dBm'},'UNIT_MISMATCH','claims[0].unit'),
            ({'artifact_id':'missing'},'UNKNOWN_ARTIFACT','claims[0].artifact_id'),
            ({'column':'made_up'},'UNSUPPORTED_CLAIM_COLUMN','claims[0].column'),
            ({'row_key':{}},'INVALID_ROW_KEY','claims[0].row_key'),
            ({'value':True},'INVALID_NUMERIC_VALUE','claims[0].value')]:
            with self.subTest(code=code),self.assertRaises(ExplanationError) as caught:
                validate_explanation({'claims':[{**base,**update}],'hypotheses':[]},[{'artifact_id':'a'}],[])
            self.assertEqual((caught.exception.code,caught.exception.location),(code,location))
        with patch('srs_helper.explanation.validate_numeric_claim',side_effect=ValueError('private contents')):
            with self.assertRaisesRegex(ExplanationError,'EVIDENCE_MISMATCH') as caught:
                validate_explanation({'claims':[base],'hypotheses':[]},[{'artifact_id':'a'}],[])
            self.assertNotIn('private',str(caught.exception))
        with self.assertRaisesRegex(ExplanationError,'UNBOUND_HYPOTHESIS'):
            validate_explanation({'claims':[],'hypotheses':[{'text':'guess','references':['missing']}]},[],[])
        self.assertEqual(validate_explanation({'claims':[],'hypotheses':[]},[],[]),([],[]))
