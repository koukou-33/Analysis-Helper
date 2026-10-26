import unittest
from srs_helper.plans import QueryPlan,validate_plan_fields,PlanFieldError


class PlanFieldTests(unittest.TestCase):
    columns = {'resource':['record_id','event_ts','crnti'],
               'stream':['record_id','event_ts','crnti','streamId','instBfSinrGain']}

    def plan(self, steps):
        return QueryPlan(dataset_id='a',dataset_fingerprint='b',question='test',steps=steps)

    def test_compare_must_reference_aggregation_relation(self):
        steps = [{'step_id':'s1','operator':'GROUP_AGG','source':'stream','group_by':['streamId'],
                  'metrics':[{'field':'instBfSinrGain','aggregate':'avg','output':'gain_avg'}]},
                 {'step_id':'s2','operator':'COMPARE','source':'stream','compare_field':'gain_avg'}]
        with self.assertRaises(PlanFieldError) as caught:
            validate_plan_fields(self.plan(steps),self.columns)
        self.assertEqual(caught.exception.details['available_in'],['s1'])
        self.assertEqual(caught.exception.details['location'],'steps[1].compare_field')
        steps[1].update(source='s1',depends_on=['s1'])
        schemas = validate_plan_fields(self.plan(steps),self.columns)
        self.assertEqual(schemas['s2'],{'streamId','sample_cnt','gain_avg','comparison_diff'})

    def test_condition_count_rows_and_unknown_semantics(self):
        steps = [{'step_id':'s1','operator':'CONDITION_COUNT','source':'stream','group_by':['streamId'],
                  'conditions':{'c1':{'op':'gt','field':'absent_feature','value':0}}},
                 {'step_id':'s2','operator':'FILTER','source':'s1_rows','depends_on':['s1'],
                  'condition':{'op':'eq','field':'c1','value':True}}]
        schemas=validate_plan_fields(self.plan(steps),self.columns)
        self.assertIn('c1',schemas['s2'])
        self.assertNotIn('instBfSinrGain',schemas['s1'])
        steps[1].update(source='s1',condition={'op':'gt','field':'instBfSinrGain','value':0})
        with self.assertRaises(PlanFieldError):
            validate_plan_fields(self.plan(steps),self.columns)

    def test_aggregation_drops_raw_fields_and_temporal_checks_both_sources(self):
        first={'step_id':'s1','operator':'GROUP_AGG','source':'stream','metrics':[{'field':'record_id','aggregate':'count','output':'n'}]}
        for second in [
            {'step_id':'s2','source':'s1','depends_on':['s1'],'operator':'GROUP_AGG','group_by':['streamId'],
             'metrics':[{'field':'n','aggregate':'sum','output':'total'}]},
            {'step_id':'s2','source':'stream','other_source':'s1','depends_on':['s1'],'operator':'TEMPORAL_RELATE','object_keys':['crnti'],'window_seconds':3}]:
            with self.assertRaises(PlanFieldError):
                validate_plan_fields(self.plan([first,second]),self.columns)
