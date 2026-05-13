"""Layer 5 — Rule-based recommended actions."""

# Priority: AI-generated -> Flagged -> high conf -> low conf -> museum/art -> unknown brand -> moderate


def generate_actions(source_type, brand, confidence_score, provenance_status, layer1_uncertain=False):
    """Return dict with `actions` as list of dicts {text, evidence?} and `severity`."""
    actions = []
    severity = "info"

    def add(text, evidence=None):
        obj = {"text": text}
        if evidence:
            obj["evidence"] = evidence
        actions.append(obj)

    st = (source_type or "").lower()
    if "ai-generated" in st:
        add("AI-generated image detected. No real product may exist.", evidence={"layer": "layer1", "reason": "ai_detected"})
        add("Do not engage with this seller or listing.")
        texts = [a["text"] for a in actions]
        return {"actions": texts, "actions_meta": actions, "severity": "critical"}

    if provenance_status == "Flagged":
        add("This image appears in our scam database.", evidence={"layer": "layer4", "reason": "db_flag"})
        add("Do not purchase. Report the listing if applicable.")
        texts = [a["text"] for a in actions]
        return {"actions": texts, "actions_meta": actions, "severity": "critical"}

    if layer1_uncertain and "uncertain" in st:
        add("Source type uncertain; treat all signals with extra caution.", evidence={"layer": "layer1", "note": "uncertain"})
        severity = "caution"

    if brand == "Unknown luxury item":
        add("Brand could not be identified from the supported list.")
        add("Review the caption and request more photos if buying.")
        severity = "caution" if severity == "info" else severity

    if confidence_score >= 75:
        add(f"High confidence signal for {brand}.", evidence={"layer": "layer3", "confidence": confidence_score})
        add("For purchases over INR 50,000, request an authentication certificate.")
        severity = "info"
    elif confidence_score < 25:
        add("Inconclusive / low confidence signal. Visual cues do not strongly match authentic references.", evidence={"layer": "layer3", "confidence": confidence_score})
        add("Request additional photos: serial numbers, stitching close-up, receipt.", evidence={"layer": "layer1", "note": "request_photos"})
        severity = "warning"
    elif confidence_score < 50:
        add("Low confidence signal. Significant visual differences possible.", evidence={"layer": "layer3", "confidence": confidence_score})
        add("Request additional photos: serial numbers, stitching close-up, receipt.", evidence={"layer": "layer1", "note": "request_photos"})
        severity = "warning"
    else:
        add("Moderate signal. Proceed with caution.", evidence={"layer": "layer3", "confidence": confidence_score})
        add("Ask seller for proof of purchase or authentication card.")
        severity = "caution"

    # Museum / art context
    art_brands = {"Monet", "Van Gogh", "Vermeer", "Rembrandt"}
    if brand in art_brands:
        actions = []
        add(f"Art context: likely {brand}-style or museum-related image.")
        add("Learn more at a trusted museum or catalogue raisonne.")
        severity = "info"

    # Screenshot without scam flag
    if "screenshot" in st and provenance_status != "Flagged":
        add("Screenshot detected — not a direct product photo; ID may be less reliable.", evidence={"layer": "layer1", "note": "screenshot"})

    # Dedupe while preserving order (by text)
    seen = set()
    unique = []
    for a in actions:
        t = a.get("text")
        if t not in seen:
            seen.add(t)
            unique.append(a)

    texts = [a.get("text") for a in unique]
    return {"actions": texts, "actions_meta": unique, "severity": severity}
