"""End-to-end 5-layer analyse(image) with optional Gradio progress."""

import io
from pathlib import Path

import numpy as np
from PIL import Image

from pipeline.layer1_source import classify_source_type
from pipeline.layer2_object import identify_object
from pipeline.layer3_confidence import confidence_signal
from pipeline.layer4_provenance import check_provenance
from pipeline.layer5_actions import generate_actions
from pipeline.models import get_blip, get_clip

MAX_BYTES = 10 * 1024 * 1024
ALLOWED_MODES = {"RGB", "RGBA", "L", "P"}


def _notify(progress, fraction, message):
    if progress is None:
        return
    try:
        progress(fraction, desc=message)
    except TypeError:
        try:
            progress(fraction, message)
        except Exception:
            pass
    except Exception:
        pass


def _to_pil(image):
    if image is None:
        raise ValueError("Please upload an image.")

    if isinstance(image, Image.Image):
        pil = image
    elif isinstance(image, (str, Path)):
        pil = Image.open(image)
    else:
        arr = np.asarray(image)
        if arr.ndim == 2:
            pil = Image.fromarray(arr).convert("RGB")
        elif arr.ndim == 3:
            pil = Image.fromarray(arr)
        else:
            raise ValueError("Unsupported image array shape.")

    if pil.mode not in ALLOWED_MODES and pil.mode != "RGB":
        pil = pil.convert("RGB")
    else:
        pil = pil.convert("RGB")

    return pil


def _check_size_and_format(pil_image, original_path=None):
    if original_path is not None:
        try:
            sz = Path(original_path).stat().st_size
            if sz > MAX_BYTES:
                raise ValueError(
                    "Image too large. Please compress below 10 MB and retry."
                )
        except OSError:
            pass

    buf = io.BytesIO()
    pil_image.save(buf, format="JPEG", quality=92)
    if buf.tell() > MAX_BYTES:
        raise ValueError(
            "Image too large. Please compress below 10 MB and retry."
        )


def analyse(image, progress=None):
    """
    Run Layers 1-5. Returns dict with layer1..layer5, warnings, global_disclaimer.
    """
    warnings = []
    pil = _to_pil(image)
    _check_size_and_format(pil)

    _notify(progress, 0.05, "Processing image...")

    _notify(progress, 0.15, "Analysing image source type...")
    clip = get_clip()
    layer1 = classify_source_type(pil, clip)

    _notify(progress, 0.35, "Identifying object and brand...")
    blip = get_blip()
    layer2 = identify_object(pil, clip, blip)

    brand_for_l3 = layer2["brand"]
    if brand_for_l3 == "Unknown luxury item":
        brand_for_l3 = "luxury item"

    _notify(progress, 0.55, "Running confidence signal...")
    layer3 = confidence_signal(pil, clip, brand_for_l3)

    _notify(progress, 0.75, "Checking provenance database...")
    layer4 = check_provenance(pil)

    _notify(progress, 0.88, "Generating recommendations...")
    layer5 = generate_actions(
        layer1["source_type"],
        layer2["brand"],
        layer3["confidence_score"],
        layer4["provenance_status"],
        layer1_uncertain=layer1.get("uncertain", False),
    )

    if layer1.get("uncertain") and layer2.get("brand") == "Unknown luxury item":
        warnings.append(
            "This image produced low-confidence results across multiple layers. "
            "Results shown but may be inaccurate."
        )

    _notify(progress, 1.0, "Analysis complete.")

    global_disclaimer = (
        "Research demo. Not a professional authentication service. "
        "Do not use for high-value purchase decisions."
    )

    return {
        "layer1": layer1,
        "layer2": layer2,
        "layer3": layer3,
        "layer4": layer4,
        "layer5": layer5,
        "warnings": warnings,
        "global_disclaimer": global_disclaimer,
    }


def format_report(result):
    """Plain-text summary for Gradio Markdown."""
    lines = []
    l1, l2, l3, l4, l5 = (
        result["layer1"],
        result["layer2"],
        result["layer3"],
        result["layer4"],
        result["layer5"],
    )
    lines.append(
        f"**Layer 1 — Source type:** {l1['source_type']} "
        f"(confidence: {l1['confidence']*100:.1f}%)"
    )
    lines.append(
        f"**Layer 2 — Object:** {l2['brand']} / {l2['category']} "
        f"(brand confidence: {l2['confidence']*100:.1f}%)  "
        f"Alt: {', '.join(l2.get('alt_guesses') or [])}"
    )
    lines.append(
        f"**Layer 3 — Confidence signal:** {l3['confidence_score']}/100 "
        f"Signal: {l3['signal_label']}"
    )
    lines.append(f"> {l3['disclaimer']}")
    lines.append(
        f"**Layer 4 — Provenance:** {l4['provenance_status']} "
        f"(demo DB: {l4.get('db_entry_count', 0)} entries)"
    )
    if l4.get("note"):
        lines.append(f"_{l4['note']}_")
    lines.append("**Layer 5 — Recommended actions:**")
    for a in l5.get("actions", []):
        lines.append(f"- {a}")
    lines.append(f"**Severity:** {l5.get('severity', 'info')}")
    for w in result.get("warnings", []):
        lines.append(f"**Warning:** {w}")
    lines.append(f"**Disclaimer:** {result['global_disclaimer']}")
    return "\n\n".join(lines)
