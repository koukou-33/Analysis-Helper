import csv
import json
import tempfile
import unittest
from pathlib import Path

import pyarrow as pa

from srs_helper.data_audit import audit_data_coverage
from srs_helper.database import open_dataset
from srs_helper.expert_tools import run_tools
from srs_helper.pipeline import build_dataset
from srs_helper.provenance import save_table, bind_cohort
from srs_helper.queries import Scope


class ToolRepairTests(unittest.TestCase):
    def run_proposals(self, proposals, **extra):
        observed, searches, saved = [], [], []
        iterator = iter(proposals)
        def propose(payload):
            observed.append(payload)
            return next(iterator)
        result = run_tools(propose=propose, payload=lambda: {}, query=lambda _: None,
                           search=lambda value: searches.append(value) or ['doc'],
                           save=lambda value: saved.append(json.loads(json.dumps(value))),
                           restored=[], remaining=lambda: 10, **extra)
        return result, observed, searches

    def test_schema_error_is_repaired_without_running_invalid_tool(self):
        result, payloads, searches = self.run_proposals([
            {'action': 'search', 'purpose': 'check', 'search': 999},
            {'action': 'search', 'purpose': 'check', 'search': 'taValue'},
            {'action': 'finish', 'purpose': 'available evidence covered'},
        ])
        self.assertEqual([x['status'] for x in result], ['INVALID_PROPOSAL', 'SUCCEEDED', 'FINISHED'])
        self.assertEqual(searches, ['taValue'])
        self.assertEqual(payloads[1]['validation_feedback'][0]['location'], ['search'])
        self.assertNotIn('input', json.dumps(result[0]))
        self.assertEqual(result[0]['attempt'], 1)

    def test_invalid_proposals_have_bound_and_resume_does_not_reset(self):
        bad = {'action': 'query', 'purpose': 'missing plan'}
        result, _, _ = self.run_proposals([bad] * 4)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[-1]['status'], 'FAILED')
        self.assertEqual(result[-1]['error'], 'INVALID_TOOL_PROPOSAL')
        remaining_proposals = []
        resumed = run_tools(propose=lambda p: remaining_proposals.append(p) or bad,
                            payload=lambda: {}, query=lambda _: None, search=lambda _: None,
                            save=lambda _: None, restored=result[:2], remaining=lambda: 10)
        self.assertEqual(resumed[-1]['attempt'], 3)
        self.assertEqual(len(remaining_proposals), 1)

    def test_execution_failure_is_not_replayed(self):
        calls = []
        def search(_):
            calls.append(1)
            raise RuntimeError('password-do-not-log')
        result = run_tools(propose=lambda p: {'action': 'search', 'purpose': 'check', 'search': 'TA'},
                           payload=lambda: {}, query=lambda _: None, search=search, save=lambda _: None,
                           restored=[], remaining=lambda: 10)
        self.assertEqual(calls, [1])
        self.assertEqual(result[-1]['failure_kind'], 'EXECUTION')
        self.assertNotIn('password', json.dumps(result))


class DataCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        source = self.root / 'NR_SRS.csv'
        with source.open('w', newline='') as file:
            writer = csv.writer(file)
            writer.writerow(['ueLayerNum','uwTtiCnt1','logicCellId','crnti','uwData','uwTime','frmNum','subFrmNum','slot','taValue'])
            writer.writerows([
                [4,100,1,11,'2026-11-12','00:00:01.000',37,0,0,18],
                [4,110,1,11,'2026-11-12','00:00:01.000',38,0,0,74],
                [4,120,2,22,'2026-11-12','00:00:01.000',37,0,0,16],
            ])
        self.directory, self.meta = build_dataset(source, self.root/'data',
            json.loads(Path('config/input_contract.json').read_text()))
        self.scope = Scope()
        self.request = {k: self.meta[k] for k in ('dataset_id', 'dataset_fingerprint')}
        self.request.update(trend=True, metrics=[{'canonical_field': 'taValue'}])
        with open_dataset(self.directory) as (con, _):
            self.ids = con.execute('SELECT logicCellId,record_id FROM l4_ue_resource_ts ORDER BY logicCellId,uwTtiCnt1').fetchall()

    def artifact(self, cells=(1, 2), fine=True):
        rows = [{'logicCellId': cell, 'value': 18.0, 'record_ids': [rid], 'eligible_count': 1}
                for cell, rid in self.ids if cell in cells]
        base = {**self.request, 'scope': self.scope.__dict__, 'quality_policy': 'EXCLUDE_ABNORMAL_RECORD'}
        result = save_table(pa.Table.from_pylist(rows), self.root/'artifacts', base, 'METRIC_TIME_SERIES')
        result['field'] = 'taValue'
        if fine:
            result['observation_version'] = 'observations.v2'
        # Deliberately truncated/misleading summary: audit must read full Parquet.
        result['group_statistics'] = [{'logicCellId': 1, 'valid_points': 99}]
        return result

    def test_full_artifact_coverage_not_truncated_summary(self):
        result = audit_data_coverage(self.directory, self.scope, self.request, [self.artifact()])
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual([r['metrics']['taValue']['points'] for r in result['per_cell']], [2, 1])
        self.assertEqual([r['source_rows'] for r in result['per_cell']], [2, 1])

    def test_missing_cell_is_a_repair_task(self):
        result = audit_data_coverage(self.directory, self.scope, self.request, [self.artifact(cells=(1,))])
        self.assertEqual(result['status'], 'NEEDS_DATA')
        self.assertEqual(result['tasks'][0]['logic_cell_ids'], [2])

    def test_coarse_evidence_is_not_passed_as_fine_observations(self):
        result = audit_data_coverage(self.directory, self.scope, self.request, [self.artifact(fine=False)])
        self.assertEqual(result['status'], 'NEEDS_DATA')
        self.assertEqual(result['tasks'][0]['reasons'], ['COARSE_TIME_COLLAPSE'])
        self.assertEqual(result['tasks'][0]['logic_cell_ids'], [1])

    def test_missing_cell_and_missing_time_are_explicit_data_limits(self):
        scope = Scope(logic_cell_id=77)
        result = audit_data_coverage(self.directory, scope, self.request, [])
        self.assertEqual(result['status'], 'LIMITED')
        self.assertEqual(result['limitations'][0]['code'], 'NO_SOURCE_ROWS')
        self.assertFalse(result['tasks'])
        contract = json.loads(Path('config/input_contract.json').read_text())
        contract['date_format'] = '%Y/%m/%d'
        directory, meta = build_dataset(self.root/'NR_SRS.csv', self.root/'wrong-date', contract)
        request = {**self.request, **{k: meta[k] for k in ('dataset_id', 'dataset_fingerprint')}}
        result = audit_data_coverage(directory, self.scope, request, [])
        self.assertEqual(result['status'], 'LIMITED')
        self.assertIn('TIME_UNAVAILABLE', {x['code'] for x in result['limitations']})
        self.assertFalse(result['tasks'])

    def test_non_trend_and_absent_request_do_not_require_charts(self):
        result = audit_data_coverage(self.directory, self.scope, {**self.request, 'trend': False}, [])
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(audit_data_coverage(self.directory, self.scope, None, [])['status'], 'NOT_REQUIRED')

    def test_real_trend_and_cohort_boundary(self):
        from srs_helper.charts import metric_time_series
        base = {**self.request, 'scope': self.scope.__dict__, 'quality_policy': 'EXCLUDE_ABNORMAL_RECORD'}
        cohort = save_table(pa.table({'record_id': [self.ids[0][1]]}), self.root/'cohorts', base, 'FILTER')
        unrelated = self.artifact()
        with bind_cohort(cohort):
            missing = audit_data_coverage(self.directory, self.scope, self.request, [unrelated])
            self.assertEqual(missing['status'], 'NEEDS_DATA')
            self.assertEqual(sum(r['source_rows'] for r in missing['per_cell']), 1)
            artifact = metric_time_series(self.directory, self.scope, self.root/'actual', 'taValue')
            result = audit_data_coverage(self.directory, self.scope, self.request, [artifact])
            self.assertEqual(result['status'], 'PASS')
            self.assertEqual(result['per_cell'][0]['metrics']['taValue']['represented_rows'], 1)
        empty = save_table(pa.table({'record_id': pa.array([], type=pa.string())}), self.root/'cohorts', base, 'FILTER')
        with bind_cohort(empty):
            result = audit_data_coverage(self.directory, self.scope, self.request, [])
            self.assertEqual(result['status'], 'LIMITED')
            self.assertEqual(result['limitations'][0]['code'], 'NO_SOURCE_ROWS')


if __name__ == '__main__':
    unittest.main()
