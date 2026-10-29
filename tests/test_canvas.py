import unittest
from srs_helper.canvas import describe_artifact, feature_figures, observation_figure


class CanvasTests(unittest.TestCase):
    def test_description_binds_own_plan_not_reused_step_id(self):
        plan={'plan_id':'original','steps':[{'step_id':'s1','operator':'CONDITION_COUNT',
              'conditions':{'c0':{'op':'lt','field':'preSinr','value_field':'spfPreSinrThd'}}}]}
        artifact={'plan_id':'original','step_id':'s1_rows','operator':'condition_evaluated_rows',
                  'row_count':10000,'query_plan':plan}
        text=describe_artifact(artifact,{'plan_id':'another','steps':[]})
        self.assertIn('preSinr 小于 spfPreSinrThd',text)
        self.assertIn('10000',text)

    def test_ratio_includes_unknown_and_blocks_failed_validation(self):
        feature={'validation':{'status':'VERIFIED'},'sample_cnt':10,
                 'flag_states':{'True':3,'False':2,'None':5}}
        chart,_=feature_figures(feature)[0]
        self.assertEqual([float(t.y[0]) for t in chart.data],[.3,.2,.5])
        feature['validation']['status']='FAILED'
        self.assertEqual(feature_figures(feature),[])
        feature['validation']['status']='VERIFIED'
        feature['sample_cnt']=0
        self.assertEqual(feature_figures(feature),[])

    def test_observations_do_not_connect_across_gaps(self):
        figure=observation_figure([{'ue_uid':'u1','event_ts':'2026-10-29T12:00:00','state':'ENABLED'},
            {'ue_uid':'u1','event_ts':'2026-10-29T12:01:00','state':'GAP'},
            {'ue_uid':'u1','event_ts':'2026-10-29T12:02:00','state':'DISABLED'}])
        self.assertTrue(all(t.mode=='markers' for t in figure.data))
        self.assertEqual({t.name for t in figure.data},{'生效','观测断点','未生效'})
