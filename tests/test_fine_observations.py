import csv
import json
import tempfile
import unittest
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import pyarrow as pa

from srs_helper.pipeline import build_dataset
from srs_helper.queries import Scope
from srs_helper.charts import metric_time_series
from srs_helper.chart_preview import chart_objects, joint_time_coverage, time_coverage
from srs_helper.canvas import metric_figures
from srs_helper.observations import create_observations


class FineObservationTests(unittest.TestCase):
    def build(self, root, rows):
        source=root/'NR_SRS.csv'
        columns=['ueLayerNum','uwTtiCnt1','logicCellId','crnti','uwData','uwTime',
                 'frmNum','subFrmNum','slot','resourceId','freqRbStart','freqRbNum','usrRsrp','taValue']
        with source.open('w',encoding='utf-8',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=columns); writer.writeheader()
            for changes in rows:
                writer.writerow({'ueLayerNum':4,'uwTtiCnt1':100,'logicCellId':1,'crnti':88,
                    'uwData':'2026-11-13','uwTime':'00:00:00.000','frmNum':37,
                    'subFrmNum':6,'slot':0,'resourceId':0,'freqRbStart':0,'freqRbNum':12,
                    'usrRsrp':-100,'taValue':18,**changes})
        directory,_=build_dataset(source,root/'data',json.loads(Path('config/input_contract.json').read_text()))
        return metric_time_series(directory,Scope(),root/'out','taValue')

    def test_same_second_changes_cells_and_joint_plot(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=self.build(Path(tmp),[
                {'uwTtiCnt1':100,'frmNum':37,'taValue':18},
                {'uwTtiCnt1':110,'frmNum':38,'taValue':74},
                {'uwTtiCnt1':120,'frmNum':39,'taValue':20},
                {'logicCellId':2,'uwTtiCnt1':500,'taValue':16},
                {'logicCellId':2,'uwTtiCnt1':510,'frmNum':38,'taValue':16}])
            points=pq.read_table(a['path']).to_pylist()
            self.assertEqual(a['statistics']['points'],5)
            self.assertEqual(a['statistics']['valid_points'],5)
            self.assertEqual(a['statistics']['observed_changes'],2)
            self.assertEqual([p['value'] for p in points if p['logicCellId']==1],[18,74,20])
            self.assertEqual(len({p['event_ts'] for p in points}),1)
            self.assertEqual(a['observation_version'],'observations.v2')
            objects=chart_objects(a)
            frame,coverage=joint_time_coverage(a,objects)
            self.assertEqual(coverage['shown_objects'],2)
            figures=metric_figures(frame,a)
            self.assertTrue(figures)
            self.assertEqual(len(figures[0][0].data),2)
            self.assertNotEqual(figures[0][0].data[0].xaxis,figures[0][0].data[1].xaxis)

    def test_true_conflict_and_quality_barrier_are_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=self.build(Path(tmp),[
                {'taValue':18},{'taValue':19},
                {'uwTtiCnt1':110,'frmNum':38,'taValue':74},
                {'uwTtiCnt1':120,'frmNum':39,'taValue':99,'usrRsrp':-170},
                {'uwTtiCnt1':130,'frmNum':40,'taValue':20}])
            rows=pq.read_table(a['path']).to_pylist()
            self.assertEqual(len(rows),4)
            self.assertTrue(rows[0]['conflict'])
            self.assertEqual(rows[0]['sample_cnt'],2)
            self.assertIsNone(rows[0]['value'])
            self.assertEqual(rows[1]['value'],74)
            self.assertTrue(rows[2]['barrier'])
            self.assertIsNone(rows[2]['value'])
            self.assertEqual(rows[3]['value'],20)
            self.assertNotEqual(rows[1]['segment'],rows[3]['segment'])
            self.assertEqual(sum(len(r['record_ids']) for r in rows),5)
            self.assertEqual(a['statistics']['eligible_rows'],4)

    def test_resources_do_not_conflict_or_join(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=self.build(Path(tmp),[{}, {'resourceId':1,'taValue':74},
                {'uwTtiCnt1':110,'frmNum':38},
                {'resourceId':1,'uwTtiCnt1':110,'frmNum':38,'taValue':75}])
            rows=pq.read_table(a['path']).to_pylist()
            self.assertEqual(len({r['series_id'] for r in rows}),2)
            self.assertEqual(a['statistics']['valid_points'],4)
            self.assertEqual(len(a['group_statistics']),2)
            self.assertEqual({r['first_value'] for r in a['group_statistics']},{18,74})

    def test_reset_repeated_positions_and_frame_wrap_break(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=self.build(Path(tmp),[
                {'uwTtiCnt1':100,'frmNum':1023},
                {'uwTtiCnt1':110,'frmNum':0,'taValue':20},
                {'uwTtiCnt1':5,'frmNum':1,'taValue':21},
                {'uwTtiCnt1':100,'frmNum':1023,'taValue':18}])
            rows=pq.read_table(a['path']).to_pylist()
            self.assertEqual(len(rows),4)
            self.assertEqual(rows[1]['ordering_status'],'FRAME_WRAP_OR_OUT_OF_ORDER')
            self.assertEqual(rows[2]['ordering_status'],'COUNTER_RESET_OR_OUT_OF_ORDER')
            self.assertNotEqual(rows[0]['segment'],rows[1]['segment'])
            self.assertNotEqual(rows[1]['segment'],rows[2]['segment'])
            self.assertIsNone(a['group_statistics'][0]['first_value'])
            self.assertIsNone(a['group_statistics'][0]['last_value'])

    def test_missing_fine_keys_do_not_merge_or_claim_order(self):
        # Loader quarantines missing TTI; direct relation covers older/partial sources.
        with duckdb.connect() as con:
            con.execute('''CREATE TABLE l3_cleaned_srs AS SELECT i::VARCHAR AS record_id,
                'x.csv' AS source_file,i AS file_row_id,TIMESTAMP '2026-11-13' AS event_ts,
                1 AS logicCellId,88 AS crnti,NULL::INT AS phyCellInst,0 AS resourceId,
                0 AS freqRbStart,12 AS freqRbNum,NULL::BIGINT AS uwTtiCnt1,
                NULL::INT AS frmNum,NULL::INT AS subFrmNum,NULL::INT AS slot,
                TRUE AS csv_structure_complete FROM range(3) t(i)''')
            con.execute("CREATE TABLE assigned_sessions AS SELECT record_id,'u' AS ue_uid FROM l3_cleaned_srs")
            con.execute('CREATE TABLE resource AS SELECT *,18.0 AS taValue FROM l3_cleaned_srs')
            create_observations(con,Scope(),'"taValue"')
            self.assertEqual(con.execute('SELECT count(*) FROM trend').fetchone()[0],3)
            self.assertEqual(con.execute("SELECT count(*) FROM trend WHERE ordering_status='UNRESOLVED_ORDER'").fetchone()[0],2)
            self.assertEqual(con.execute('SELECT count(DISTINCT segment) FROM trend').fetchone()[0],3)

    def test_parent_cohort_excludes_other_rows_and_barriers(self):
        from srs_helper.provenance import active_cohort
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            a=self.build(root,[{}, {'uwTtiCnt1':110,'frmNum':38,'taValue':74,'usrRsrp':-170},
                              {'uwTtiCnt1':120,'frmNum':39,'taValue':20}])
            first=pq.read_table(a['path']).to_pylist()[0]['record_ids'][0]
            token=active_cohort.set(pa.table({'record_id':[first]}))
            try:
                directory=next((root/'data').glob('*/analysis.duckdb')).parent
                child=metric_time_series(directory,Scope(),root/'child','taValue')
            finally:
                active_cohort.reset(token)
            rows=pq.read_table(child['path']).to_pylist()
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['record_ids'],[first])
            self.assertFalse(rows[0]['barrier'])
            self.assertEqual(child['statistics']['source_rows'],1)

    def test_multiple_days_keep_same_frame_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=self.build(Path(tmp),[{}, {'uwData':'2026-11-14','taValue':74}])
            rows=pq.read_table(a['path']).to_pylist()
            self.assertEqual(len(rows),2)
            self.assertEqual({r['value'] for r in rows},{18,74})
            self.assertEqual(len({r['ue_uid'] for r in rows}),2)

    def test_sampling_keeps_each_resource_and_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=self.build(Path(tmp),[{'resourceId':resource,'uwTtiCnt1':100+i,'frmNum':37+i,'taValue':i}
                                    for i in range(30) for resource in (0,1)])
            objects=chart_objects(a)
            frame,coverage=time_coverage(a,*objects[0],limit=12)
            self.assertLessEqual(len(frame),12)
            self.assertEqual(set(frame.resourceId),{0,1})
            for _,part in frame.groupby('series_id'):
                self.assertEqual(part.observation_index.min(),1)
                self.assertEqual(part.observation_index.max(),30)


if __name__=='__main__':
    unittest.main()
