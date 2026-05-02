import csv
import sys
from pathlib import Path
import imagehash
from PIL import Image

ROOT = Path(__file__).resolve().parent
FLAGGED = ROOT / "flagged"
OUT = ROOT / "scam_database.csv"
EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def main():
    rows = []
    if FLAGGED.exists():
        for f in sorted(FLAGGED.iterdir()):
            if f.suffix.lower() not in EXTS:
                continue
            try:
                img = Image.open(f).convert("RGB")
            except Exception as e:
                print("skip", f, e, file=sys.stderr)
                continue
            ph = imagehash.phash(img)
            rows.append(
                {
                    "phash_value": str(ph),
                    "platform": "Demo",
                    "scam_type": "counterfeit_or_ai",
                    "reported_date": "2026-01-01",
                    "note": f.name,
                }
            )
    with open(OUT, "w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(
            fp,
            fieldnames=[
                "phash_value",
                "platform",
                "scam_type",
                "reported_date",
                "note",
            ],
        )
        w.writeheader()
        w.writerows(rows)
    print("Wrote", len(rows), "rows to", OUT)


if __name__ == "__main__":
    main()