import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Default candidate labels for CLIP zero-shot (PRD brand list)
DEFAULT_BRAND_CLIP_LABELS: List[str] = [
    "Chanel designer handbag",
    "Hermes designer handbag",
    "Louis Vuitton designer handbag",
    "Gucci designer handbag",
    "Fendi designer handbag",
    "Prada designer handbag",
    "Rolex luxury wristwatch",
    "Patek Philippe luxury wristwatch",
    "Cartier luxury wristwatch",
    "Audemars Piguet luxury wristwatch",
    "Monet impressionist oil painting",
    "Van Gogh impressionist oil painting",
    "Vermeer Dutch golden age painting",
    "Rembrandt Dutch golden age painting",
    "Faberge ornate jewelled egg or objet",
    "Tiffany fine jewellery",
    "Van Cleef and Arpels fine jewellery",
]

DEFAULT_LABEL_TO_BRAND_CATEGORY: Dict[str, Tuple[str, str]] = {
    "Chanel designer handbag": ("Chanel", "Handbag"),
    "Hermes designer handbag": ("Hermes", "Handbag"),
    "Louis Vuitton designer handbag": ("Louis Vuitton", "Handbag"),
    "Gucci designer handbag": ("Gucci", "Handbag"),
    "Fendi designer handbag": ("Fendi", "Handbag"),
    "Prada designer handbag": ("Prada", "Handbag"),
    "Rolex luxury wristwatch": ("Rolex", "Watch"),
    "Patek Philippe luxury wristwatch": ("Patek Philippe", "Watch"),
    "Cartier luxury wristwatch": ("Cartier", "Watch"),
    "Audemars Piguet luxury wristwatch": ("Audemars Piguet", "Watch"),
    "Monet impressionist oil painting": ("Monet", "Art"),
    "Van Gogh impressionist oil painting": ("Van Gogh", "Art"),
    "Vermeer Dutch golden age painting": ("Vermeer", "Art"),
    "Rembrandt Dutch golden age painting": ("Rembrandt", "Art"),
    "Faberge ornate jewelled egg or objet": ("Faberge", "Jewellery"),
    "Tiffany fine jewellery": ("Tiffany", "Jewellery"),
    "Van Cleef and Arpels fine jewellery": ("Van Cleef", "Jewellery"),
}

BRAND_CONFIDENCE_THRESHOLD = 0.35

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
BRAND_LABELS_JSON = DATA_DIR / "brand_labels.json"


def _load_brand_assets() -> Tuple[List[str], Dict[str, Tuple[str, str]]]:
    if not BRAND_LABELS_JSON.exists():
        return DEFAULT_BRAND_CLIP_LABELS, DEFAULT_LABEL_TO_BRAND_CATEGORY

    try:
        payload = json.loads(BRAND_LABELS_JSON.read_text(encoding="utf-8"))
        clip_labels = payload.get("clip_labels")
        mapping = payload.get("label_to_brand_category")
        if not isinstance(clip_labels, list) or not isinstance(mapping, dict):
            return DEFAULT_BRAND_CLIP_LABELS, DEFAULT_LABEL_TO_BRAND_CATEGORY

        # mapping values can come as lists; normalize to tuples.
        normalized: Dict[str, Tuple[str, str]] = {}
        for k, v in mapping.items():
            if isinstance(v, (list, tuple)) and len(v) == 2:
                normalized[str(k)] = (str(v[0]), str(v[1]))

        clip_labels2 = [str(x) for x in clip_labels if isinstance(x, (str, int, float))]
        if not clip_labels2 or not normalized:
            return DEFAULT_BRAND_CLIP_LABELS, DEFAULT_LABEL_TO_BRAND_CATEGORY

        return clip_labels2, normalized
    except Exception:
        return DEFAULT_BRAND_CLIP_LABELS, DEFAULT_LABEL_TO_BRAND_CATEGORY


def identify_object(image, clip_pipe, blip_pipe) -> Dict[str, Any]:
    """Layer 2: caption (BLIP) + brand/category (CLIP)."""
    caption_result = blip_pipe(image)
    caption = caption_result[0]["generated_text"].strip()

    brand_clip_labels, label_to_brand_category = _load_brand_assets()

    brand_result = clip_pipe(image, candidate_labels=brand_clip_labels)
    top3 = brand_result[:3]
    top = top3[0]
    top_label = top["label"]
    top_score = float(top["score"])

    if top_score < BRAND_CONFIDENCE_THRESHOLD:
        return {
            "caption": caption,
            "brand": "Unknown luxury item",
            "category": "Unknown",
            "confidence": round(top_score, 4),
            "alt_guesses": [r["label"] for r in top3],
            "clip_label": top_label,
        }

    brand, category = label_to_brand_category.get(
        top_label, (top_label.split()[0], "Unknown")
    )
    alt_guesses = [r["label"] for r in top3[1:3]]

    return {
        "caption": caption,
        "brand": brand,
        "category": category,
        "confidence": round(top_score, 4),
        "alt_guesses": alt_guesses,
        "clip_label": top_label,
    }