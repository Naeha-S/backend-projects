"""Layer 5 — Rule-based recommended actions."""

# Priority: AI-generated -> Flagged -> high conf -> low conf -> museum/art -> unknown brand -> moderate


def generate_actions(source_type, brand, confidence_score, provenance_status, layer1_uncertain=False):
    actions = []
    severity = "info"

    st = (source_type or "").lower()
    if "ai-generated" in st:
        return {
            "actions": [
                "AI-generated image detected. No real product may exist.",
                "Do not engage with this seller or listing.",
            ],
            "severity": "critical",
        }

    if provenance_status == "Flagged":
        return {
            "actions": [
                "This image appears in our scam database.",
                "Do not purchase. Report the listing if applicable.",
            ],
            "severity": "critical",
        }

    if layer1_uncertain and "uncertain" in st:
        actions.append("Source type uncertain; treat all signals with extra caution.")
        severity = "caution"

    if brand == "Unknown luxury item":
        actions.extend(
            [
                "Brand could not be identified from the supported list.",
                "Review the caption and request more photos if buying.",
            ]
        )
        severity = "caution" if severity == "info" else severity

    if confidence_score >= 75:
        actions.extend(
            [
                f"High confidence signal for {brand}.",
                "For purchases over INR 50,000, request an authentication certificate.",
            ]
        )
        severity = "info"
    elif confidence_score < 25:
        actions.extend(
            [
                "Inconclusive / low confidence signal. Visual cues do not strongly match authentic references.",
                "Request additional photos: serial numbers, stitching close-up, receipt.",
            ]
        )
        severity = "warning"
    elif confidence_score < 50:
        actions.extend(
            [
                "Low confidence signal. Significant visual differences possible.",
                "Request additional photos: serial numbers, stitching close-up, receipt.",
            ]
        )
        severity = "warning"
    else:
        actions.extend(
            [
                "Moderate signal. Proceed with caution.",
                "Ask seller for proof of purchase or authentication card.",
            ]
        )
        severity = "caution"

    # Museum / art context
    art_brands = {"Monet", "Van Gogh", "Vermeer", "Rembrandt"}
    if brand in art_brands:
        actions = [
            f"Art context: likely {brand}-style or museum-related image.",
            "Learn more at a trusted museum or catalogue raisonne.",
        ]
        severity = "info"

    # Screenshot without scam flag
    if "screenshot" in st and provenance_status != "Flagged":
        actions.append("Screenshot detected — not a direct product photo; ID may be less reliable.")

    # Dedupe while preserving order
    seen = set()
    unique = []
    for a in actions:
        if a not in seen:
            seen.add(a)
            unique.append(a)

    return {"actions": unique[:5], "severity": severity}
