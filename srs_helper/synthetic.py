"""Seeded synthetic captures: copy the header only, never source measurements."""
import csv
import hashlib
import json
import random
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path


def generate(template, output, seed=20261022, blocks=50, files=2, profile="normal"):
    if blocks < 1 or files < 1 or profile not in {"normal", "mixed"}:
        raise ValueError("Positive files/blocks and normal/mixed profile required")
    with Path(template).open(encoding="utf-8-sig", newline="") as stream:
        header = next(csv.reader(stream))
    fields = ["uwTtiCnt1", "logicCellId", "crnti", "uwDate", "uwTime", "slotId",
              "l2PhyCellInst", "freqUserRbStart", "freqUserRbNum", "resourceId", "frmNum",
              "subFrmNum", "usrRsrp", "srsUeNiDb"] + [f"instBfSinrGain[{i}]" for i in range(4)]
    counts = Counter(header)
    if any(counts[n] != 1 for n in fields):
        raise ValueError("Template must have exactly one of every mapped calculation field")
    positions = {n: header.index(n) for n in fields}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    rng = random.Random(seed)
    manifest = {"synthetic": True, "generator_version": 1, "seed": seed, "profile": profile,
                "header_sha256": hashlib.sha256(json.dumps(header).encode()).hexdigest(),
                "normal_gain_rule": "stream0 > stream1 > stream2 > stream3; dB; no scaling",
                "unknown_fields": "left empty; no source data copied", "files": [], "truth": []}
    start = datetime(2026, 10, 21, 23, 58, 0)
    for file_id in range(files):
        name = f"NR_SRS.synthetic_{file_id:02d}.csv"
        manifest["files"].append(name)
        with (output / name).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            for block in range(blocks):
                scenario = "normal" if profile == "normal" else {
                    3: "gain_missing", 4: "gain_reversal", 5: "suspicious_rsrp",
                    6: "gain_parse_error", 7: "flow_truncated", 8: "all_crnti_missing",
                    9: "measurement_missing"}.get(block % 10, "normal")
                for idx in range(4):
                    number = (file_id * blocks + block) * 4 + idx
                    timestamp = start + timedelta(seconds=number * 3)
                    gains = [round(rng.uniform(14, 20), 3)]
                    for _ in range(3):
                        gains.append(round(gains[-1] - rng.uniform(1, 3), 3))
                    values = dict(uwTtiCnt1=block, logicCellId=idx % 2, crnti=1000+idx,
                                  uwDate=timestamp.strftime("%Y/%m/%d"), uwTime=timestamp.strftime("%H:%M:%S"),
                                  slotId=idx % 2, l2PhyCellInst=70+idx % 2, freqUserRbStart=(block % 2)*24,
                                  freqUserRbNum=24, resourceId=block % 2, frmNum=(number//20) % 1024,
                                  subFrmNum=number % 10, usrRsrp=round(rng.uniform(-115, -85), 3),
                                  srsUeNiDb=round(rng.uniform(-120, -95), 3))
                    if scenario == "gain_missing":
                        gains[2] = ""
                    elif scenario == "gain_reversal":
                        gains[0], gains[3] = gains[3], gains[0]
                    elif scenario == "suspicious_rsrp":
                        values["usrRsrp"] = -50
                    elif scenario == "gain_parse_error":
                        gains[1] = "NaN" if idx % 2 else "invalid"
                    elif scenario == "measurement_missing":
                        values["srsUeNiDb"] = ""
                    if scenario == "all_crnti_missing" or (scenario == "flow_truncated" and idx == 3):
                        values["crnti"] = ""
                    values.update({f"instBfSinrGain[{i}]": v for i, v in enumerate(gains)})
                    row = [""] * len(header)
                    for field, value in values.items():
                        row[positions[field]] = value
                    writer.writerow(row)
                    manifest["truth"].append({"source_file": name, "file_row_id": block*4+idx+1,
                                              "scenario": scenario, "flow_valid": scenario != "all_crnti_missing"
                                              and (scenario != "flow_truncated" or idx < 2)})
    (output / "synthetic_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
