import csv
import json
import tempfile
import unittest
from pathlib import Path

from srs_helper.pipeline import build_dataset
from srs_helper.evidence import feature_analysis
from srs_helper.queries import Scope


class ExtraFieldTests(unittest.TestCase):
    def test_real_scalar_feature_flag_and_full_rule(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root/"NR_SRS.flag.csv"
            with path.open("w",newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["uwTtiCnt1","logicCellId","crnti","uwData","uwTime","usrRsrp","srsUeNiDb","featureB"])
                writer.writerow([1,0,10,"2026-10-22","00:00:00.000",-100,-110,0])
                writer.writerow([2,0,10,"2026-10-22","00:00:01.000",-100,-110,1])
                writer.writerow([3,0,10,"2026-10-22","00:00:02.000",-100,-110,""])
            directory,meta = build_dataset(path,root/"store",json.loads(Path("config/input_contract.json").read_text()))
            source = {"document_id":"synthetic","revision":"1","section":"full rule"}
            feature = {"feature_id":"B","title":"Synthetic B","flag_field":"featureB","conditions":[
                {"condition_id":"c1","role":"SUFFICIENT","source":source,"expression":{"op":"gt","field":"usrRsrp","value":-120}}],
                "activation_logic":{"op":"eq","field":"c1","value":True},"activation_source":source}
            result = feature_analysis(directory,Scope(),meta,feature,root/"artifacts")
            self.assertEqual(result["activation_states"],{"True":3})
            self.assertEqual(result["expected_but_disabled"],1)
            self.assertEqual(result["flag_states"],{"False":1,"True":1,"None":1})
