"""Layer 1 — Image source type (CLIP zero-shot)."""

SOURCE_LABELS = [
    "real photograph",
    "AI-generated image",
    "digital screenshot",
    "3D render or CGI",
]

UNCERTAIN_THRESHOLD = 0.45


def classify_source_type(image, clip_pipe):
    result = clip_pipe(image, candidate_labels=SOURCE_LABELS)
    top = result[0]
    label = top["label"]
    score = float(top["score"])

    label_map = {
        "real photograph": "Real photograph",
        "AI-generated image": "AI-generated image",
        "digital screenshot": "Digital screenshot",
        "3D render or CGI": "3D render or CGI",
    }
    source_type = label_map.get(label, label)
    uncertain = score < UNCERTAIN_THRESHOLD
    if uncertain:
        source_type = "Uncertain"

    return {
        "source_type": source_type,
        "confidence": round(score, 4),
        "uncertain": uncertain,
        "raw_label": label,
        "raw_scores": {r["label"]: float(r["score"]) for r in result[:4]},
    }
