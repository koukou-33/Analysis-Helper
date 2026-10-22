import tempfile
import unittest
from pathlib import Path

import pyarrow.parquet as pq

import test_synthetic_queries as fixtures
from srs_helper.evidence import feature_analysis, validate_numeric_claim
from srs_helper.plans import Expression, Metric, QueryPlan, Step, execute_plan
from srs_helper.queries import Scope


class PlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.SyntheticQueryTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def plan(self, steps):
        return QueryPlan(dataset_id=self.normal_meta["dataset_id"],dataset_fingerprint=self.normal_meta["dataset_fingerprint"],question="test",steps=steps)

    def run_plan(self, steps):
        return execute_plan(self.normal_db,Scope(logic_cell_id=0),self.plan(steps),self.root/"results")

    def test_filter_group_compare_and_claim(self):
        results = self.run_plan([
            Step(step_id="s1",operator="FILTER",source="stream",condition=Expression(op="eq",field="crnti",value=1000)),
            Step(step_id="s2",operator="GROUP_AGG",source="s1",depends_on=["s1"],group_by=["streamId"],
                 metrics=[Metric(field="instBfSinrGain",aggregate="avg",output="gain")]),
            Step(step_id="s3",operator="COMPARE",source="s2",depends_on=["s2"],compare_field="gain")])
        self.assertEqual(results[-1]["row_count"],4)
        row = results[-1]["preview"][0]
        validate_numeric_claim(results[-1],{"streamId":row["streamId"]},"gain",row["gain"],"dB","dB")
        with self.assertRaises(ValueError):
            validate_numeric_claim(results[-1],{"streamId":row["streamId"]},"gain",row["gain"]+1,"dB","dB")
        with self.assertRaises(ValueError):
            validate_numeric_claim(results[-1],{"streamId":row["streamId"]},"gain",row["gain"],"dBm","dB")

    def test_conditions_unknown_and_rows_dependency(self):
        results = self.run_plan([
            Step(step_id="s1",operator="CONDITION_COUNT",conditions={"c1":Expression(op="gt",field="missing_field",value=0),
                                                                    "c2":Expression(op="gt",field="usrRsrp",value=-160)}),
            Step(step_id="s2",operator="FILTER",source="s1_rows",depends_on=["s1"],condition=Expression(op="eq",field="c2",value=True))])
        self.assertEqual(results[1]["preview"][0]["c1_unknown"],40)
        self.assertEqual(results[-1]["row_count"],40)

    def test_claim_checks_rows_beyond_preview(self):
        results = self.run_plan([Step(step_id="s1",operator="FILTER",source="stream",condition=Expression(op="ge",field="streamId",value=0))])
        artifact = results[0]
        self.assertTrue(artifact["preview_truncated"])
        last = pq.read_table(artifact["path"]).to_pylist()[-1]
        self.assertTrue(validate_numeric_claim(artifact,{"record_id":last["record_id"],"streamId":last["streamId"]},
                                               "instBfSinrGain",last["instBfSinrGain"],"dB","dB"))

    def test_temporal_slice_no_multiplication(self):
        results = self.run_plan([
            Step(step_id="s1",operator="TIME_SLICE",start="2026-10-21T23:58:00",end="2026-10-21T23:59:00"),
            Step(step_id="s2",operator="TEMPORAL_RELATE",source="s1",other_source="s1",depends_on=["s1"],
                 object_keys=["logicCellId","crnti"],window_seconds=12)])
        self.assertEqual(results[0]["row_count"],10)
        self.assertEqual(results[1]["row_count"],10)

    def test_bad_plans_rejected(self):
        with self.assertRaises(ValueError):
            self.plan([Step(step_id="s1",operator="FILTER",source="read_csv('secret')")])
        with self.assertRaises(ValueError):
            self.run_plan([Step(step_id="s1",operator="GROUP_AGG",metrics=[Metric(field="read_csv('secret')",aggregate="avg",output="bad")])])
        with self.assertRaises(ValueError):
            self.run_plan([Step(step_id="s1",operator="TEMPORAL_RELATE",other_source="resource",object_keys=["crnti"])])

    def test_necessary_is_not_sufficient(self):
        source = {"document_id":"test","revision":"1","section":"1"}
        feature = {"feature_id":"B","title":"B","flag_field":"absent_B", "conditions":[
            {"condition_id":"c1","role":"NECESSARY","expression":{"op":"gt","field":"usrRsrp","value":-160},"source":source}]}
        result = feature_analysis(self.normal_db,Scope(),self.normal_meta,feature,self.root/"feature")
        self.assertEqual(result["activation_states"],{"None":80})
        self.assertEqual(result["flag_states"],{"None":80})
        self.assertEqual(result["expected_but_disabled"],0)
