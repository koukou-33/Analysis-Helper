import csv
import json
import tempfile
import unittest
from pathlib import Path

from srs_helper.pipeline import build_dataset
from srs_helper.evidence import feature_analysis
from srs_helper.queries import Scope


class ExtraFieldTests(unittest.TestCase):
    def test_duplicate_spf_columns_merge_per_row(self):
        from srs_helper.database import open_dataset
        from srs_helper.plans import supplemental_columns, prepare_scope
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); path=root/'NR_SRS.duplicate.csv'
            with path.open('w',newline='') as stream:
                writer=csv.writer(stream)
                writer.writerow(['ueLayerNum','uwTtiCnt1','logicCellId','crnti','uwData','uwTime','srsSpfSwitch','srsSpfSwitch'])
                for i,values in enumerate([['',1],[0,''],[1,1],['',''],[0,1]]):
                    writer.writerow([4,i,0,10,'2026-10-27','00:00:00.000',*values])
            directory,meta=build_dataset(path,root/'store',json.loads(Path('config/input_contract.json').read_text()))
            self.assertEqual(meta['rule_version'],'m1.5')
            with open_dataset(directory) as (con,_):
                self.assertIn('srsSpfSwitch',supplemental_columns(con))
                prepare_scope(con,Scope(),['srsSpfSwitch'])
                self.assertEqual(con.execute('SELECT srsSpfSwitch FROM resource ORDER BY file_row_id').fetchall(),[(1,),(0,),(1,),(None,),(None,)])
                self.assertEqual(con.execute("SELECT count(*) FROM l2_extra_numeric WHERE parse_state='DUPLICATE_VALUE_CONFLICT'").fetchone()[0],1)
                self.assertEqual(con.execute("SELECT count(*) FROM field_catalog WHERE raw_name='srsSpfSwitch'").fetchone()[0],2)
                self.assertIn('"", "1"',con.execute('SELECT raw_values FROM l2_standardized WHERE file_row_id=1').fetchone()[0])

    def test_real_scalar_feature_flag_and_full_rule(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root/"NR_SRS.flag.csv"
            with path.open("w",newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["ueLayerNum","uwTtiCnt1","logicCellId","crnti","uwData","uwTime","usrRsrp","srsUeNiDb","featureB"])
                writer.writerow([4,1,0,10,"2026-10-22","00:00:00.000",-100,-110,0])
                writer.writerow([4,2,0,10,"2026-10-22","00:00:01.000",-100,-110,1])
                writer.writerow([4,3,0,10,"2026-10-22","00:00:02.000",-100,-110,""])
            directory,meta = build_dataset(path,root/"store",json.loads(Path("config/input_contract.json").read_text()))
            source = {"document_id":"synthetic","revision":"1","section":"full rule"}
            feature = {"feature_id":"B","title":"Synthetic B","flag_field":"featureB","conditions":[
                {"condition_id":"c1","role":"SUFFICIENT","source":source,"expression":{"op":"gt","field":"usrRsrp","value":-120}}],
                "activation_logic":{"op":"eq","field":"c1","value":True},"activation_source":source}
            result = feature_analysis(directory,Scope(),meta,feature,root/"artifacts")
            self.assertEqual(result["activation_states"],{"True":3})
            self.assertEqual(result["expected_but_disabled"],1)
            self.assertEqual(result["flag_states"],{"False":1,"True":1,"None":1})
