"""Layer 4 — Perceptual hash vs demo scam database."""

import csv
from pathlib import Path

import imagehash

THRESHOLD = 10

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CSV_PATH = DATA_DIR / "scam_database.csv"


def _load_rows():
    if not CSV_PATH.exists():
        return []
    rows = []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("phash_value"):
                rows.append(row)
    return rows


def check_provenance(image):
    """
    image: PIL Image
    Returns provenance_status, match_source, match_date, note, db_entry_count
    """
    rows = _load_rows()
    n = len(rows)
    query_hash = imagehash.phash(image)

    for row in rows:
        try:
            stored = imagehash.hex_to_hash(row["phash_value"])
        except Exception:
            continue
        if query_hash - stored < THRESHOLD:
            return {
                "provenance_status": "Flagged",
                "match_source": row.get("platform", ""),
                "match_date": row.get("reported_date", ""),
                "note": row.get("note", ""),
                "scam_type": row.get("scam_type", ""),
                "db_entry_count": n,
            }

    return {
        "provenance_status": "Clean",
        "match_source": "",
        "match_date": "",
        "note": (
            f"No match found in demo database ({n} entries). "
            "This does not guarantee the image is safe."
        ),
        "scam_type": "",
        "db_entry_count": n,
    }
