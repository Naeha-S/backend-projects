"""Optional offline step: build data/brand_labels.json from yixiannn/luxury-products-data.

This is NOT required for the Space to run. The Space will fall back to the default
hardcoded brand list if data/brand_labels.json is missing.

Usage:
  python scripts/build_brand_labels.py
"""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "brand_labels.json"

# Small allowlist to avoid polluting CLIP with arbitrary retailer brands.
# You can expand this later.
ALLOW = {
    "Chanel",
    "Hermes",
    "Louis Vuitton",
    "Gucci",
    "Fendi",
    "Prada",
    "Rolex",
    "Patek Philippe",
    "Cartier",
    "Audemars Piguet",
    "Monet",
    "Van Gogh",
    "Vermeer",
    "Rembrandt",
    "Faberge",
    "Tiffany",
    "Van Cleef",
}


def main() -> int:
    try:
        from datasets import load_dataset
    except Exception as e:
        raise SystemExit(
            "datasets is not installed. Run: pip install -r requirements-dev.txt"
        ) from e

    ds = load_dataset("yixiannn/luxury-products-data", split="train")

    brands = set()
    if "Marque" in ds.column_names:
        for b in ds["Marque"]:
            if not b:
                continue
            brands.add(str(b).strip())

    # Keep only allowlisted ones (plus exact matches).
    selected = sorted([b for b in brands if b in ALLOW])

    # Build CLIP labels (keep same style as defaults).
    clip_labels = []
    mapping = {}

    def add(label: str, brand: str, category: str):
        if label not in clip_labels:
            clip_labels.append(label)
        mapping[label] = [brand, category]

    for b in selected:
        # Heuristic category choices.
        if b in {"Rolex", "Patek Philippe", "Cartier", "Audemars Piguet"}:
            add(f"{b} luxury wristwatch", b, "Watch")
        elif b in {"Monet", "Van Gogh", "Vermeer", "Rembrandt"}:
            add(f"{b} oil painting", b, "Art")
        elif b in {"Faberge", "Tiffany", "Van Cleef"}:
            add(f"{b} fine jewellery", b, "Jewellery")
        else:
            add(f"{b} designer handbag", b, "Handbag")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "source": "yixiannn/luxury-products-data",
                "clip_labels": clip_labels,
                "label_to_brand_category": mapping,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"Wrote {len(clip_labels)} labels to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())