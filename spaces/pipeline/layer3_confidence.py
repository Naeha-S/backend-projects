"""Layer 3 — CLIP-based confidence signal (authentic vs replica wording)."""

DISCLAIMER = (
    "This confidence signal is based on CLIP visual similarity — not professional "
    "authentication. Do not use for purchases over INR 50,000 without expert verification."
)


def _signal_label(score):
    if score >= 75:
        return "High"
    if score >= 50:
        return "Moderate"
    if score >= 25:
        return "Low"
    return "Inconclusive"


def confidence_signal(image, clip_pipe, brand_name):
    """brand_name: first token of brand for label phrases (e.g. Chanel)."""
    if not brand_name or brand_name == "Unknown luxury item":
        brand_name = "luxury item"

    pos_labels = [
        f"authentic {brand_name}",
        f"genuine {brand_name}",
        f"real {brand_name} item",
    ]
    neg_labels = [
        f"replica {brand_name}",
        f"counterfeit {brand_name}",
        f"fake {brand_name}",
    ]
    all_labels = pos_labels + neg_labels
    result = clip_pipe(image, candidate_labels=all_labels)

    pos_score = 0.0
    neg_score = 0.0
    for r in result:
        lab = r["label"]
        sc = float(r["score"])
        if lab in pos_labels:
            pos_score += sc
        elif lab in neg_labels:
            neg_score += sc

    denom = pos_score + neg_score
    if denom <= 0:
        confidence_score = 50
    else:
        confidence_score = int(round((pos_score / denom) * 100))

    return {
        "confidence_score": confidence_score,
        "signal_label": _signal_label(confidence_score),
        "disclaimer": DISCLAIMER,
        "pos_score": round(pos_score, 4),
        "neg_score": round(neg_score, 4),
    }
