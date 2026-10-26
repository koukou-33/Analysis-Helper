import csv
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from srs_helper.pipeline import build_dataset
from srs_helper.queries import Scope, summarize
from srs_helper.synthetic import generate


class SyntheticQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.template = cls.root / "template.csv"
        cls.header = ["uwTtiCnt1", "logicCellId", "crnti", "uwDate", "uwTime", "slotId",
                      "l2PhyCellInst", "freqUserRbStart", "freqUserRbNum", "resourceId", "frmNum",
                      "subFrmNum", "usrRsrp", "srsUeNiDb", "ueLayerNum", "ueSymPos"] + [f"instBfSinrGain[{i}]" for i in range(4)] + ["tag", "tag"]
        with cls.template.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(cls.header)
            writer.writerow(["PRIVATE_SOURCE_VALUE"] * len(cls.header))
        cls.normal = cls.root / "normal"
        cls.mixed = cls.root / "mixed"
        cls.normal_truth = generate(cls.template, cls.normal, blocks=10, files=2)
        cls.mixed_truth = generate(cls.template, cls.mixed, blocks=10, files=1, profile="mixed")
        contract = json.loads(Path("config/sample_input_contract.json").read_text())
        cls.normal_db, cls.normal_meta = build_dataset(cls.normal, cls.root / "datasets", contract)
        cls.mixed_db, _ = build_dataset(cls.mixed, cls.root / "datasets", contract)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_normal_order_reproducibility_and_no_source_copy(self):
        repeat = self.root / "repeat"
        generate(self.template, repeat, blocks=10, files=2)
        for path in self.normal.glob("*.csv"):
            self.assertEqual(path.read_bytes(), (repeat / path.name).read_bytes())
            self.assertNotIn("PRIVATE_SOURCE_VALUE", path.read_text())
            with path.open(newline="") as stream:
                reader = csv.reader(stream)
                self.assertEqual(next(reader), self.header)
                for row in reader:
                    gains = [float(row[self.header.index(f"instBfSinrGain[{i}]")]) for i in range(4)]
                    self.assertTrue(all(a > b for a, b in zip(gains, gains[1:])))
        with self.assertRaises(FileExistsError):
            generate(self.template, self.normal)

    def test_flow_truth_and_quality_cases(self):
        result = summarize(self.mixed_db)
        self.assertEqual(result["flow_valid_count"], sum(r["flow_valid"] for r in self.mixed_truth["truth"]))
        self.assertEqual(result["flow_valid_count"], 34)
        self.assertEqual(result["quality"]["suspicious_count"], 4)
        self.assertEqual(sum(s["gain_parse_error_cnt"] for s in result["streams"]), 4)
        self.assertEqual(sum(s["gain_missing_cnt"] for s in result["streams"]), 4)

    def test_filtered_recompute_matches_csv_oracle(self):
        start, end = datetime(2026,10,21,23,58,24), datetime(2026,10,21,23,59,36)
        result = summarize(self.normal_db, Scope(logic_cell_id=0,crnti=1000,resource_id=0,start=start,end=end))
        selected = []
        for path in self.normal.glob("*.csv"):
            with path.open(newline="") as stream:
                for r in csv.DictReader(stream):
                    ts = datetime.strptime(r["uwDate"]+" "+r["uwTime"], "%Y-%m-%d %H:%M:%S")
                    if int(r["logicCellId"]) == 0 and int(r["crnti"]) == 1000 and int(r["resourceId"]) == 0 and start <= ts < end:
                        selected.append(r)
        self.assertEqual(result["flow_valid_count"], len(selected))
        self.assertGreater(len(selected), 0)
        group = result["groups"][0]
        self.assertEqual(group["ue_total_records"], len(selected))
        self.assertAlmostEqual(group["rsrp_avg"], sum(float(r["usrRsrp"]) for r in selected)/len(selected))
        for i, actual in enumerate(group["stream_gain_means"]):
            self.assertAlmostEqual(actual, sum(float(r[f"instBfSinrGain[{i}]"]) for r in selected)/len(selected))
        self.assertLess(len(selected), summarize(self.normal_db)["flow_valid_count"])

    def test_time_boundary_identity_and_parameter_binding(self):
        result = summarize(self.normal_db, Scope(start=datetime(2026,10,21,23,58,0),end=datetime(2026,10,21,23,58,3)))
        self.assertEqual(result["flow_valid_count"], 1)
        self.assertEqual(summarize(self.normal_db, Scope(ue_uid="' OR 1=1 --"))["flow_valid_count"], 0)
        with self.assertRaises(ValueError):
            summarize(self.normal_db, dataset_id="another dataset")
        with self.assertRaises(ValueError):
            summarize(self.normal_db, Scope(start=datetime(2026,1,2),end=datetime(2026,1,1)))
        with self.assertRaises(ValueError):
            summarize(self.normal_db, Scope(start=datetime(2026,1,1,tzinfo=timezone.utc)))
        result = summarize(self.normal_db, Scope(logic_cell_id=999))
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["streams"], [])


if __name__ == "__main__":
    unittest.main()
