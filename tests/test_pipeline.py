import csv
import json
import tempfile
import unittest
from pathlib import Path

import duckdb

from srs_helper.pipeline import build_dataset


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inputs = self.root / "input"
        self.inputs.mkdir()
        self.contract = json.loads(Path("config/input_contract.json").read_text())

    def write(self, rows, name="NR_SRS.test.csv", extra_header=()):
        path = self.inputs / name
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["uwTtiCnt1", "logicCellId", "crnti", "uwData", "uwTime", "usrRsrp", "srsUeNiDb", *extra_header])
            writer.writerows(rows)
        return path

    def row(self, tti=1, crnti="10", time="00:00:00.000", rsrp="-100", ni="-110", date="20261021"):
        return [tti, 1, crnti, date, time, rsrp, ni]

    def build(self):
        path, meta = build_dataset(self.inputs, self.root / "storage", self.contract)
        con = duckdb.connect(str(path / "analysis.duckdb"), read_only=True)
        self.addCleanup(con.close)
        return con, meta

    def test_complete_and_repeated_blocks(self):
        self.write([self.row(10), self.row(10), self.row(11), self.row(10)])
        c, m = self.build()
        self.assertEqual(m["status"], "READY")
        self.assertEqual(c.execute("SELECT count(*),count(DISTINCT block_seq) FROM l3_cleaned_srs WHERE flow_status='VALID_FLOW'").fetchone(), (4, 3))

    def test_incomplete_prefix_boundary_and_all_empty(self):
        self.write([self.row(crnti="10"), self.row(crnti=""), self.row(crnti="11"), self.row(crnti=""), self.row(2, "")])
        c, _ = self.build()
        self.assertEqual(c.execute("SELECT file_row_id FROM l4_ue_resource_ts").fetchall(), [(1,)])

    def test_parse_presence_quality_and_finite_mean(self):
        self.write([self.row(crnti="bad", rsrp="-50", ni=""), self.row(2, rsrp="nan"), self.row(3)])
        c, _ = self.build()
        self.assertEqual(c.execute("SELECT count(*),avg(usrRsrp) FROM l4_ue_resource_ts").fetchone(), (3, -75))
        self.assertEqual(c.execute("SELECT crnti_present,is_suspicious,has_missing_measurement,data_quality_tag,ue_uid FROM l4_ue_resource_ts WHERE file_row_id=1").fetchone(), (True, True, True, "PARSE_ERROR", None))
        self.assertEqual(c.execute("SELECT rsrp_missing,has_parse_error FROM l4_ue_resource_ts WHERE file_row_id=2").fetchone(), (False, True))

    def test_session_gap_threshold(self):
        self.write([self.row(time=t) for t in ["00:00:00.000", "00:03:00.000", "00:06:00.001"]])
        c, _ = self.build()
        self.assertEqual(c.execute("SELECT count(DISTINCT ue_uid) FROM l4_ue_resource_ts").fetchone()[0], 2)

    def test_dedup_partial_failure_and_file_isolation(self):
        p = self.write([self.row(), self.row(crnti="")])
        (self.inputs / "NR_SRS.copy.csv").write_bytes(p.read_bytes())
        self.write([self.row(crnti="99")], "NR_SRS.other.csv")
        (self.inputs / "NR_SRS.broken.csv").write_text('uwTtiCnt1,crnti\n1,"broken', encoding="utf-8")
        c, m = self.build()
        self.assertEqual(m["status"], "PARTIAL_READY")
        self.assertEqual(sum(e["status"] == "DUPLICATE" for e in m["files"]), 1)
        self.assertEqual(c.execute("SELECT count(*) FROM l4_ue_resource_ts").fetchone()[0], 1)

    def test_invalid_key_breaks_block_and_invalid_date_kept(self):
        self.write([self.row(), self.row(tti=""), self.row(date="bad")])
        c, _ = self.build()
        self.assertEqual(c.execute("SELECT count(*),count(DISTINCT block_seq) FROM l3_cleaned_srs").fetchone(), (2, 2))
        self.assertEqual(c.execute("SELECT count(*) FROM quarantined_rows").fetchone()[0], 1)
        self.assertEqual(c.execute("SELECT count(*) FROM l4_ue_resource_ts").fetchone()[0], 2)

    def test_embedded_newline_and_short_measurement(self):
        self.write([self.row() + ["a\nb"], self.row(2)[:-1]], extra_header=["note"])
        c, _ = self.build()
        self.assertEqual(c.execute("SELECT file_row_id FROM l4_ue_resource_ts ORDER BY file_row_id").fetchall(), [(1,), (2,)])
        self.assertFalse(c.execute("SELECT csv_structure_complete FROM l4_ue_resource_ts WHERE file_row_id=2").fetchone()[0])

    def test_cross_midnight_and_restore_without_source(self):
        self.write([self.row(time="00:00:01.000", date="20261022"), self.row(time="23:59:59.000")])
        c, _ = self.build()
        self.assertEqual(c.execute("SELECT max(time_sec) FROM l4_ue_resource_ts").fetchone()[0], 2)
        for path in self.inputs.iterdir():
            path.unlink()
        self.assertEqual(c.execute("SELECT count(*) FROM l4_ue_resource_ts").fetchone()[0], 2)

    def test_file_transaction_rolls_back_prior_batches(self):
        path = self.write([self.row()] * 2001)
        with path.open("a", encoding="utf-8") as f:
            f.write('1,1,"unterminated')
        c, m = self.build()
        self.assertEqual(m["status"], "FAILED")
        self.assertEqual(c.execute("SELECT count(*) FROM l2_standardized").fetchone()[0], 0)

    def test_four_stream_means_and_scalar_denominator(self):
        gains = [f"instBfSinrGain[{i}]" for i in range(4)]
        self.write([self.row(rsrp="-50") + [1, 2, 4, 8],
                    self.row(2, time="00:00:02.000", rsrp="-100") + [1, 2, 4, 8]], extra_header=gains)
        c, _ = self.build()
        self.assertEqual(c.execute("SELECT count(*) FROM l4_ue_stream_ts").fetchone()[0], 8)
        self.assertEqual(c.execute("SELECT sample_cnt,rsrp_avg,max_stream_gain_diff,rsrp_trend_delta,ue_total_records,available_stream_count FROM l4_ue_summary").fetchone(),
                         (2, -75, 7, -50, 2, 4))

    def test_missing_stream_index_and_nonfinite(self):
        self.write([self.row() + [1, "inf", 8], self.row(2) + [3, "bad", ""]],
                   extra_header=["instBfSinrGain[0]", "instBfSinrGain[2]", "instBfSinrGain[3]"])
        c, _ = self.build()
        self.assertEqual(c.execute("SELECT streamId,gain_avg,gain_valid_cnt,gain_missing_cnt,gain_parse_error_cnt FROM l4_ue_stream_summary ORDER BY streamId").fetchall(),
                         [(0, 2, 2, 0, 0), (1, None, 0, 2, 0), (2, None, 0, 0, 2), (3, 8, 1, 1, 0)])
        self.assertEqual(c.execute("SELECT max_stream_gain_diff,available_streams FROM l4_ue_summary").fetchone(), (6, [0, 3]))

    def test_no_gain_samples_returns_null(self):
        self.write([self.row()])
        c, _ = self.build()
        self.assertEqual(c.execute("SELECT max_stream_gain_diff,available_stream_count FROM l4_ue_summary").fetchone(), (None, 0))

    def test_sample_aliases_duplicate_auxiliary_and_catalog(self):
        self.contract = json.loads(Path("config/sample_input_contract.json").read_text())
        path = self.inputs / "NR_SRS.example.csv"
        header = ["uwTtiCnt1", "logicCellId", "crnti", "uwDate", "uwTime", "slotId", "slot", "tag", "tag", "instBfSinrGain[0]"] + [f"beam[{i}]" for i in range(32)] + ["other[0]"]
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerow([1, 0, 10, "2026/10/11", "10:11:21", 1, "", "a", "b", 40] + [1]*32 + [4])
        c, _ = self.build()
        self.assertEqual(c.execute("SELECT slot,year(event_ts) FROM l4_ue_resource_ts").fetchone(), (1, 2026))
        self.assertEqual(c.execute("SELECT count(*) FROM field_catalog WHERE domain='BEAM_PROVISIONAL'").fetchone()[0], 32)
        self.assertEqual(c.execute("SELECT domain FROM field_catalog WHERE raw_name='instBfSinrGain[0]'").fetchone()[0], "STREAM")
        self.assertEqual(c.execute("SELECT count(*) FROM field_catalog WHERE raw_name='tag'").fetchone()[0], 2)
        self.assertEqual(c.execute("SELECT domain FROM field_catalog WHERE raw_name='other[0]'").fetchone()[0], "UNCONFIRMED_ARRAY")

    def test_duplicate_calculation_field_rejected(self):
        self.write([self.row() + [20]], extra_header=["crnti"])
        _, meta = self.build()
        self.assertEqual(meta["status"], "FAILED")
        self.assertIn("Ambiguous mapped field", meta["files"][0]["error"])


if __name__ == "__main__":
    unittest.main()
