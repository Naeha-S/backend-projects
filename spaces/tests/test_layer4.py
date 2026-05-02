import csv
from pathlib import Path

import imagehash
import pytest
from PIL import Image

import pipeline.layer4_provenance as l4


def make_img(color):
    return Image.new("RGB", (64, 64), color=color)


def test_layer4_flagged_and_clean(tmp_path, monkeypatch):
    # Build a tiny temp CSV with one known pHash.
    flagged = make_img((255, 0, 0))
    flagged_hash = str(imagehash.phash(flagged))

    csv_path = tmp_path / "scam_database.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["phash_value", "platform", "scam_type", "reported_date", "note"],
        )
        w.writeheader()
        w.writerow(
            {
                "phash_value": flagged_hash,
                "platform": "Test",
                "scam_type": "counterfeit",
                "reported_date": "2026-01-01",
                "note": "unit",
            }
        )

    monkeypatch.setattr(l4, "CSV_PATH", csv_path)

    r1 = l4.check_provenance(flagged)
    assert r1["provenance_status"] == "Flagged"
    assert r1["match_source"] == "Test"

    clean = make_img((0, 255, 0))
    r2 = l4.check_provenance(clean)
    assert r2["provenance_status"] == "Clean"
    assert "demo database" in r2["note"].lower()