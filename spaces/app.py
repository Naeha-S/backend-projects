import html
import time
import csv
from collections import deque
import json
import os
import tempfile
import traceback
from pathlib import Path

import gradio as gr

from pipeline.orchestrator import analyse

# Keep a short in-memory history of recent analyses (most recent first)
HISTORY: deque = deque(maxlen=5)
HISTORY_PATH = Path(__file__).resolve().parent / ".lth_history.json"


def _load_history():
    try:
        if HISTORY_PATH.exists():
            with open(HISTORY_PATH, "r", encoding="utf-8") as fh:
                arr = json.load(fh)
            # maintain order most recent first
            HISTORY.clear()
            for item in arr[:HISTORY.maxlen]:
                HISTORY.append(item)
    except Exception:
        pass


# Load existing history on import
_load_history()


def _save_history():
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as fh:
            json.dump(list(HISTORY), fh, ensure_ascii=False, indent=2)
    except Exception:
        pass


def export_json_to_pdf(json_path: str) -> str:
    # Minimal PDF export using reportlab: write summary and key fields
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    if not json_path:
        raise ValueError("No JSON path provided")
    with open(json_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    pdf_path = Path(tempfile.gettempdir()) / f"luxury_truth_lens_report_{int(time.time())}.pdf"
    c = canvas.Canvas(str(pdf_path), pagesize=A4)
    w, h = A4
    margin = 20 * mm
    x = margin
    y = h - margin
    # Title
    c.setFont("Helvetica-Bold", 18)
    c.drawString(x, y, "Luxury Truth Lens — Report")
    y -= 12 * mm
    # Layers
    l2 = data.get("layer2", {})
    l3 = data.get("layer3", {})
    l4 = data.get("layer4", {})
    l5 = data.get("layer5", {})
    c.setFont("Helvetica-Bold", 12)
    c.drawString(x, y, "Brand:")
    c.setFont("Helvetica", 12)
    c.drawString(x + 40 * mm, y, str(l2.get("brand", "-")))
    y -= 8 * mm
    c.setFont("Helvetica-Bold", 12)
    c.drawString(x, y, "Category:")
    c.setFont("Helvetica", 12)
    c.drawString(x + 40 * mm, y, str(l2.get("category", "-")))
    y -= 8 * mm
    c.setFont("Helvetica-Bold", 12)
    c.drawString(x, y, "Confidence:")
    c.setFont("Helvetica", 12)
    c.drawString(x + 40 * mm, y, f"{l3.get('confidence_score', 0)}/100")
    y -= 12 * mm
    c.setFont("Helvetica-Bold", 12)
    c.drawString(x, y, "Provenance:")
    c.setFont("Helvetica", 12)
    c.drawString(x + 40 * mm, y, str(l4.get("provenance_status", "-")))
    y -= 12 * mm
    # Actions
    c.setFont("Helvetica-Bold", 12)
    c.drawString(x, y, "Actions:")
    y -= 8 * mm
    c.setFont("Helvetica", 11)
    actions = l5.get("actions", [])
    if isinstance(actions, list):
        for act in actions:
            text = act["text"] if isinstance(act, dict) and act.get("text") else str(act)
            # wrap
            for chunk in [text[i:i+80] for i in range(0, len(text), 80)]:
                if y < margin + 20 * mm:
                    c.showPage()
                    y = h - margin
                c.drawString(x + 6 * mm, y, chunk)
                y -= 6 * mm
    c.showPage()
    c.save()
    return str(pdf_path)



def _confidence_breakdown_html(l3: dict, l2: dict, l4: dict) -> str:
    # Accepts layer3 dict and builds a 3-component breakdown: visual, caption, provenance
    vs = int(l3.get("visual_similarity", l3.get("confidence_score", 0) * 0.6))
    ct = int(l2.get("confidence", 0) * 100 * 0.3) if l2.get("confidence") is not None else int((l3.get("confidence_score", 0)) * 0.2)
    pv = int(l4.get("match_score", 0)) if l4.get("match_score") is not None else 0
    # Normalize to max 100
    vs = min(100, vs)
    ct = min(100, ct)
    pv = min(100, pv)
    return (
        '<div class="breakdown">'
        f'<div class="breakdown-row"><div class="breakdown-label">Visual similarity</div><div class="breakdown-bar"><div class="breakdown-fill" style="width:{vs}%;"></div></div><div class="breakdown-val">{vs}%</div></div>'
        f'<div class="breakdown-row"><div class="breakdown-label">Caption match</div><div class="breakdown-bar"><div class="breakdown-fill" style="width:{ct}%;"></div></div><div class="breakdown-val">{ct}%</div></div>'
        f'<div class="breakdown-row"><div class="breakdown-label">Provenance match</div><div class="breakdown-bar"><div class="breakdown-fill" style="width:{pv}%;"></div></div><div class="breakdown-val">{pv}%</div></div>'
        '</div>'
    )


def _risk_matrix_html(source_type: str, score: int) -> str:
    # Map source type to an X coordinate (0 left safe, 100 right risky)
    src = (source_type or "").lower()
    if "ai" in src or "generated" in src:
        x = 85
    elif "screenshot" in src:
        x = 60
    elif "render" in src:
        x = 70
    else:
        x = 20
    # Y coordinate from confidence (low confidence => high risk on Y)
    y = 100 - score
    # Constrain
    x = max(5, min(95, x))
    y = max(5, min(95, y))
    # Simple SVG 120x120 with grid and dot
    svg = (
        f'<svg width="160" height="120" viewBox="0 0 100 75" preserveAspectRatio="none">'
        '<rect x="0" y="0" width="100" height="75" fill="rgba(255,255,255,0.02)" />'
        # axes
        '<line x1="0" y1="75" x2="100" y2="75" stroke="rgba(255,255,255,0.04)" />'
        '<line x1="0" y1="0" x2="0" y2="75" stroke="rgba(255,255,255,0.04)" />'
        # dot
        f'<circle cx="{x}" cy="{y * 0.75}" r="3.2" fill="rgba(255,90,95,0.9)" stroke="white" stroke-opacity="0.08"/> '
        f'<text x="4" y="10" font-size="6" fill="var(--fg-soft)">Low source risk</text>'
        f'<text x="68" y="10" font-size="6" fill="var(--fg-soft)">High source risk</text>'
        f'<text x="4" y="68" font-size="6" fill="var(--fg-soft)">High confidence</text>'
        f'<text x="68" y="68" font-size="6" fill="var(--fg-soft)">Low confidence</text>'
        '</svg>'
    )
    return f'<div class="risk-matrix">{svg}</div>'


def export_json_to_csv(json_path: str) -> str:
    if not json_path:
        raise ValueError("No JSON path provided")
    with open(json_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    rows = []
    l1 = data.get("layer1", {})
    l2 = data.get("layer2", {})
    l3 = data.get("layer3", {})
    l4 = data.get("layer4", {})
    l5 = data.get("layer5", {})
    rows.append(
        {
            "timestamp": time.time(),
            "brand": l2.get("brand"),
            "category": l2.get("category"),
            "source_type": l1.get("source_type"),
            "confidence_score": l3.get("confidence_score"),
            "signal_label": l3.get("signal_label"),
            "provenance_status": l4.get("provenance_status"),
            "actions": " | ".join(l5.get("actions", [])),
        }
    )
    csv_path = Path(tempfile.gettempdir()) / f"luxury_truth_lens_report_{int(time.time())}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=rows[0].keys())
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    return str(csv_path)


TOP_DISCLAIMER = (
    "Research tool, not a substitute for professional authentication. "
    "Do not rely on it alone for high-value purchase decisions."
)


def _example_paths():
    base = Path(__file__).resolve().parent / "examples"
    if not base.is_dir():
        return []
    return [
        [str(path)]
        for path in sorted(base.iterdir())
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
    ]


def _hf_token_status():
    token = (
        os.getenv("HF_TOKEN")
        or os.getenv("HUGGING_FACE_HUB_TOKEN")
        or os.getenv("HUGGINGFACEHUB_API_TOKEN")
    )
    if token:
        return "Detected"
    return "Missing"


def _severity_label(severity: str) -> str:
    sev = (severity or "info").lower()
    labels = {
        "info": "Measured confidence",
        "caution": "Guarded assessment",
        "warning": "Elevated risk",
        "critical": "Immediate concern",
    }
    return labels.get(sev, "Guarded assessment")


def _severity_class(severity: str) -> str:
    sev = (severity or "info").lower()
    if sev not in {"info", "caution", "warning", "critical"}:
        sev = "caution"
    return sev


def _confidence_band(score: int) -> str:
    if score >= 75:
        return "high"
    if score >= 45:
        return "medium"
    return "low"


def _confidence_tone(score: int) -> str:
    if score >= 75:
        return "high"
    if score >= 45:
        return "medium"
    return "low"


def _status_badge(label: str, kind: str) -> str:
    safe_kind = kind if kind in {"info", "caution", "warning", "critical", "success"} else "info"
    return f'<span class="status-badge status-{safe_kind}">{html.escape(label)}</span>'


def _summary_metric(label: str, value: str, tone: str = "info") -> str:
    return (
        f'<div class="summary-metric summary-{tone}">'
        f'<span class="summary-metric-label">{html.escape(label)}</span>'
        f'<span class="summary-metric-value">{html.escape(value)}</span>'
        f'</div>'
    )


def _confidence_visual_html(score: int, signal_label: str) -> str:
    tone = _confidence_tone(score)
    return (
        f'<div class="confidence-wrap confidence-{tone}">'
        f'<div class="confidence-head">'
        f'<span class="confidence-label">Confidence read</span>'
        f'<span class="confidence-score">{score}/100</span>'
        f'</div>'
        f'<div class="confidence-track" role="img" aria-label="Confidence {score} out of 100">'
        f'<div class="confidence-fill confidence-{tone}" style="width:{score}%;"></div>'
        f'</div>'
        f'<div class="confidence-foot">{html.escape(signal_label)}</div>'
        f'</div>'
    )


def _layer_panel_html(title: str, note: str, body: str, accent: str) -> str:
    return (
        f'<div class="layer-card layer-{accent}">'
        f'<div class="layer-card-head">'
        f'<div class="layer-card-title">{html.escape(title)}</div>'
        f'<div class="layer-card-note">{html.escape(note)}</div>'
        f'</div>'
        f'<div class="layer-card-body">{body}</div>'
        f'</div>'
    )


def _summary_html(severity: str, brand: str, category: str, score: int, provenance: str, actions: list[str]) -> str:
    severity_class = _severity_class(severity)
    confidence_band = _confidence_band(score)
    action_count = len(actions)
    provenance_kind = "success" if provenance.lower() == "clean" else "warning"
    return (
        f'<div class="summary-panel summary-{severity_class}">'
        f'<div class="summary-header">'
        f'<div>'
        f'<div class="summary-kicker">Decision read</div>'
        f'<div class="summary-title">{html.escape(_severity_label(severity))}</div>'
        f'</div>'
        f'{_status_badge(provenance, provenance_kind)}'
        f'</div>'
        f'{_confidence_visual_html(score, _severity_label(severity))}'
        f'<div class="summary-grid">'
        f'{_summary_metric("Maison", brand, "info")}'
        f'{_summary_metric("Category", category, "info")}'
        f'{_summary_metric("Confidence", f"{score}/100", confidence_band)}'
        f'{_summary_metric("Provenance", provenance, provenance_kind)}'
        f'{_summary_metric("Recommended actions", f"{action_count} item(s)", "caution")}'
        f'{_summary_metric("Overall posture", _severity_label(severity), severity_class)}'
        f'</div>'
        f'</div>'
    )


def _bullet_lines(items):
    if not items:
        return ["- (none)"]
    return [f"- {item}" for item in items]


def _empty_response(message: str, status_text: str):
    return (
        f"## Review Unavailable\n\n{message}",
        "",
        "",
        "",
        "",
        "",
        f"**Token status:** `{_hf_token_status()}`\n\n**Disclaimer:** {TOP_DISCLAIMER}",
        status_text,
        None,
    )


def run_analysis(image, progress=gr.Progress(track_tqdm=False)):
    if image is None:
        return _empty_response(
            "Please upload a JPG, PNG, or WebP image under 10 MB.",
            "Status: awaiting image.",
        )

    try:
        result = analyse(image, progress=progress)
    except ValueError as exc:
        return _empty_response(str(exc), "Status: input rejected.")
    except Exception:
        tb = traceback.format_exc()
        return (
            f"## Review Unavailable\n\nThe pipeline failed while processing this image.\n\n**Traceback (most recent call last):**\n```text\n{tb}\n```",
            "",
            "",
            "",
            "",
            "",
            f"**Token status:** `{_hf_token_status()}`\n\n**Disclaimer:** {TOP_DISCLAIMER}",
            "Status: processing failed.",
            None,
        )

    l1 = result["layer1"]
    l2 = result["layer2"]
    l3 = result["layer3"]
    l4 = result["layer4"]
    l5 = result["layer5"]

    actions = l5.get("actions", [])
    warnings = result.get("warnings", [])
    severity = l5.get("severity", "info")

    summary = _summary_html(
        severity=severity,
        brand=l2["brand"],
        category=l2["category"],
        score=l3["confidence_score"],
        provenance=l4["provenance_status"],
        actions=actions,
    )

    if warnings:
        warning_items = "".join(f"<li>{html.escape(item)}</li>" for item in warnings)
        summary += (
            '<div class="layer-note" style="margin-top:12px;">'
            '<strong>Review notes</strong>'
            f'<ul style="margin:8px 0 0 18px; padding:0;">{warning_items}</ul>'
            '</div>'
        )

    md1 = (
        f"<div class='layer-kv'><strong>Image origin</strong><span>{html.escape(l1['source_type'])}</span></div>"
        f"<div class='layer-kv'><strong>Classifier confidence</strong><span>{l1['confidence'] * 100:.1f}%</span></div>"
        f"<div class='layer-kv'><strong>Flagged as uncertain</strong><span>{'Yes' if l1.get('uncertain') else 'No'}</span></div>"
    )

    alt_guesses = ", ".join(l2.get("alt_guesses") or []) or "-"
    md2 = (
        f"<div class='layer-kv'><strong>Caption</strong><span>{html.escape(l2['caption'])}</span></div>"
        f"<div class='layer-kv'><strong>Maison</strong><span>{html.escape(l2['brand'])}</span></div>"
        f"<div class='layer-kv'><strong>Category</strong><span>{html.escape(l2['category'])}</span></div>"
        f"<div class='layer-kv'><strong>Brand confidence</strong><span>{l2['confidence'] * 100:.1f}%</span></div>"
        f"<div class='layer-kv'><strong>Alternate reads</strong><span>{html.escape(alt_guesses)}</span></div>"
    )

    md3 = (
        f"{_confidence_visual_html(l3['confidence_score'], l3['signal_label'])}"
        f"<div class='layer-note layer-note-emphasis'>{html.escape(l3['disclaimer'])}</div>"
    )

    provenance_rows = [
        ("Status", l4["provenance_status"]),
        ("Reference matches", str(l4.get("db_entry_count", 0))),
    ]
    if l4.get("match_source"):
        provenance_rows.append(("Reference source", l4["match_source"]))
    if l4.get("match_date"):
        provenance_rows.append(("Recorded date", l4["match_date"]))
    if l4.get("note"):
        provenance_rows.append(("Note", l4["note"]))
    md4 = "".join(
        f"<div class='layer-kv'><strong>{html.escape(label)}</strong><span>{html.escape(value)}</span></div>"
        for label, value in provenance_rows
    )

    action_items_list = []
    for item in actions:
        if isinstance(item, dict):
            text = item.get("text") or item.get("label") or "(action)"
            evidence = item.get("evidence")
            if evidence and isinstance(evidence, dict):
                ev_layer = evidence.get("layer") or evidence.get("source") or ""
                ev_note = evidence.get("note") or evidence.get("id") or ""
                ev_html = f" <span class='evidence' style='color:var(--muted); font-size:0.85rem;'>(via {html.escape(ev_layer)} {html.escape(str(ev_note))})</span>"
            else:
                ev_html = ""
            action_items_list.append(f"<li>{html.escape(str(text))}{ev_html}</li>")
        else:
            action_items_list.append(f"<li>{html.escape(str(item))}</li>")
    action_items = "".join(action_items_list) or "<li>(none)</li>"
    md5 = f"<ul class='action-list'>{action_items}</ul>"

    meta = (
        f"**Token status:** `{_hf_token_status()}`\n\n"
        f"**Disclaimer:** {result.get('global_disclaimer', TOP_DISCLAIMER)}"
    )

    json_path = Path(tempfile.gettempdir()) / "luxury_truth_lens_report.json"
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)

    # Record in-memory history (keep recent 5)
    try:
        HISTORY.appendleft(
            {
                "time": int(time.time()),
                "path": str(json_path),
                "brand": l2.get("brand"),
                "score": l3.get("confidence_score"),
                "severity": severity,
            }
        )
        # persist
        _save_history()
    except Exception:
        # non-fatal
        pass

    # Confidence breakdown and risk matrix (phase 3)
    breakdown_html = _confidence_breakdown_html(l3, l2, l4)
    matrix_html = _risk_matrix_html(l1.get("source_type", ""), l3.get("confidence_score", 0))

    # Attach breakdown into md3 display and include risk matrix near summary
    md3 = (
        f"{_confidence_visual_html(l3['confidence_score'], l3['signal_label'])}"
        f"{breakdown_html}"
        f"<div style=\"margin-top:12px;\">{matrix_html}</div>"
        f"<div class='layer-note layer-note-emphasis'>{html.escape(l3.get('disclaimer',''))}</div>"
    )

    # Prepare recent labels for the dropdown (most recent first)
    recent_labels = [f"{item.get('brand') or 'unknown'} - {item.get('score')}/100" for item in list(HISTORY)]

    return (
        summary,
        md1,
        md2,
        md3,
        md4,
        md5,
        meta,
        "Status: review complete.",
        str(json_path),
        recent_labels,
        summary,  # also return summary HTML as recent_summary preview
    )


def build_ui():
    css = """
    @import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600;700&family=Manrope:wght@400;500;600;700;800&display=swap');

    :root {
        --bg: #050505;
        --panel: #0f0f10;
        --panel-2: #151516;
        --panel-3: #1b1b1d;
        --ink: #f5f1e8;
        --ink-soft: #d5cdbc;
        --muted: #9e947f;
        --line: #2a261f;
        --line-strong: #4a4032;
        --accent: #c4a46d;
        --accent-soft: #877154;
        --success: #8ca07a;
        --warn: #c79b62;
        --danger: #b86b5d;
        --shadow: 0 22px 60px rgba(0, 0, 0, 0.42);
        --radius-card: 22px;
        --radius-base: 16px;
        --radius-sm: 10px;
    }

    html { scroll-behavior: smooth; }
    *, *::before, *::after { box-sizing: border-box; }

    body,
    .gradio-container,
    .gradio-container > .main,
    .gradio-container > .main > .wrap,
    footer {
        background: var(--bg) !important;
        color: var(--ink) !important;
        font-family: 'Manrope', sans-serif !important;
        border: none !important;
    }

    .gradio-container {
        width: min(1720px, 97vw) !important;
        max-width: none !important;
        padding: 20px 20px 48px !important;
    }

    h1, h2, h3, h4 {
        color: var(--ink) !important;
        font-family: 'Cormorant Garamond', serif !important;
        font-weight: 600;
        letter-spacing: -0.03em;
    }

    .masthead-row {
        align-items: end !important;
        gap: 18px !important;
        margin-bottom: 18px !important;
    }

    .hero-shell {
        display: none !important;
    }

    .masthead {
        display: grid;
        grid-template-columns: minmax(0, 1.2fr) 240px;
        gap: 24px;
        align-items: end;
        padding: 4px 2px 14px;
        border-bottom: 1px solid rgba(196, 164, 109, 0.16);
    }

    .masthead-mark {
        color: var(--muted);
        font-size: 11px;
        font-weight: 800;
        letter-spacing: 0.22em;
        text-transform: uppercase;
        margin-bottom: 14px;
    }

    .masthead-title {
        margin: 0;
        font-size: clamp(3.8rem, 6.6vw, 7.2rem);
        line-height: 0.82;
        letter-spacing: -0.045em;
        text-wrap: balance;
    }

    .masthead-copy {
        max-width: 860px;
        margin-top: 12px;
        color: var(--ink-soft);
        font-size: 1.02rem;
        line-height: 1.7;
    }

    .masthead-side {
        align-self: stretch;
        display: flex;
        flex-direction: column;
        justify-content: flex-end;
        gap: 10px;
        padding-left: 22px;
        border-left: 1px solid rgba(196, 164, 109, 0.16);
    }

    .masthead-side-label {
        color: var(--muted);
        font-size: 10px;
        font-weight: 800;
        letter-spacing: 0.18em;
        text-transform: uppercase;
    }

    .masthead-side-value {
        color: var(--ink);
        font-family: 'Cormorant Garamond', serif !important;
        font-size: 1.8rem;
        line-height: 0.95;
    }

    .soft-status {
        min-height: 116px;
        padding: 18px 20px;
        background: var(--panel) !important;
        border: 1px solid var(--line) !important;
        border-radius: var(--radius-base);
        color: var(--ink-soft) !important;
        font-size: 0.92rem;
        font-weight: 600;
        display: flex;
        align-items: flex-end;
    }

    .workspace-row {
        gap: 18px !important;
        align-items: stretch !important;
    }

    .soft-card {
        padding: 22px;
        background: var(--panel) !important;
        border: 1px solid var(--line) !important;
        border-radius: var(--radius-card) !important;
        box-shadow: var(--shadow) !important;
    }

    .submission-card {
        position: sticky;
        top: 16px;
    }

    .section-title {
        margin: 0 0 4px;
        color: var(--ink) !important;
        font-size: 2.7rem;
        line-height: 0.9;
        letter-spacing: -0.04em;
    }

    .section-copy {
        margin: 0 0 18px;
        color: var(--ink-soft);
        line-height: 1.65;
        font-size: 0.94rem;
    }

    .well {
        padding: 10px;
        background: var(--panel-2) !important;
        border: 1px solid var(--line) !important;
        border-radius: var(--radius-base);
    }

    .submission-meta {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 10px;
        margin: 14px 0 16px;
    }

    .meta-chip {
        padding: 12px 14px;
        background: var(--panel-2);
        border: 1px solid var(--line);
        border-radius: var(--radius-sm);
    }

    .meta-chip strong {
        display: block;
        margin-bottom: 6px;
        color: var(--muted);
        font-size: 10px;
        font-weight: 800;
        letter-spacing: 0.18em;
        text-transform: uppercase;
    }

    .summary-box {
        margin: 0 0 14px;
        padding: 14px;
        background: var(--panel-2) !important;
        border: 1px solid var(--line) !important;
        border-radius: var(--radius-base);
    }

    .summary-panel {
        padding: 22px;
        background: var(--panel-3);
        border: 1px solid var(--line);
        border-radius: var(--radius-base);
    }

    .summary-panel.summary-critical { border-color: rgba(184, 107, 93, 0.85); }
    .summary-panel.summary-warning { border-color: rgba(199, 155, 98, 0.85); }
    .summary-panel.summary-caution { border-color: rgba(164, 141, 101, 0.85); }
    .summary-panel.summary-info { border-color: rgba(196, 164, 109, 0.72); }

    .summary-header {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 16px;
        margin-bottom: 18px;
    }

    .summary-kicker,
    .summary-metric-label,
    .confidence-label,
    .layer-kv strong,
    .gradio-container label > span,
    .gradio-container .label-wrap > span {
        color: var(--muted) !important;
        font-size: 10px !important;
        font-weight: 800 !important;
        letter-spacing: 0.18em !important;
        text-transform: uppercase !important;
    }

    .summary-title,
    .layer-card-title {
        color: var(--ink);
        font-family: 'Cormorant Garamond', serif !important;
        font-size: 2rem;
        line-height: 0.98;
        font-weight: 600;
    }

    .summary-grid {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 12px;
    }

    .summary-metric {
        min-height: 96px;
        padding: 15px 16px;
        background: var(--panel) !important;
        border: 1px solid var(--line);
        border-radius: var(--radius-sm);
    }

    .summary-metric-value {
        display: block;
        color: var(--ink);
        font-size: 1rem;
        line-height: 1.45;
        font-weight: 700;
    }

    .summary-critical { border-color: rgba(184, 107, 93, 0.7); }
    .summary-warning { border-color: rgba(199, 155, 98, 0.72); }
    .summary-caution { border-color: rgba(164, 141, 101, 0.72); }
    .summary-info { border-color: rgba(196, 164, 109, 0.62); }
    .summary-high { border-color: rgba(140, 160, 122, 0.72); }
    .summary-medium { border-color: rgba(199, 155, 98, 0.72); }
    .summary-low { border-color: rgba(184, 107, 93, 0.72); }

    .status-badge {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        white-space: nowrap;
        padding: 8px 12px;
        border: 1px solid var(--line-strong);
        border-radius: 999px;
        font-size: 10px;
        font-weight: 800;
        letter-spacing: 0.14em;
        text-transform: uppercase;
    }

    .status-success {
        color: #d5dfca;
        background: rgba(140, 160, 122, 0.12);
        border-color: rgba(140, 160, 122, 0.36);
    }

    .status-warning {
        color: #ead7b8;
        background: rgba(199, 155, 98, 0.12);
        border-color: rgba(199, 155, 98, 0.36);
    }

    .status-critical {
        color: #e8c4bc;
        background: rgba(184, 107, 93, 0.12);
        border-color: rgba(184, 107, 93, 0.36);
    }

    .status-info {
        color: var(--ink-soft);
        background: rgba(196, 164, 109, 0.1);
        border-color: rgba(196, 164, 109, 0.28);
    }

    .confidence-wrap {
        margin-bottom: 16px;
        padding: 16px;
        background: var(--panel) !important;
        border: 1px solid var(--line);
        border-radius: var(--radius-sm);
    }

    .confidence-head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        margin-bottom: 12px;
    }

    .confidence-score {
        color: var(--ink);
        font-family: 'Manrope', sans-serif !important;
        font-size: 1.15rem;
        font-weight: 800;
    }

    .confidence-track,
    .breakdown-bar {
        overflow: hidden;
        background: #090909;
        border: 1px solid var(--line);
        border-radius: 999px;
    }

    .confidence-track {
        width: 100%;
        height: 12px;
    }

    .confidence-fill,
    .breakdown-fill {
        height: 100%;
        border-radius: 999px;
        transition: width 320ms ease;
    }

    .confidence-fill.high,
    .breakdown-fill { background: #b79a67; }
    .confidence-fill.medium { background: #8c7a5d; }
    .confidence-fill.low { background: #8f5d54; }

    .confidence-foot,
    .layer-card-note,
    .layer-note,
    .soft-footer,
    .breakdown-label {
        color: var(--ink-soft) !important;
        font-size: 0.88rem;
        line-height: 1.65;
    }

    .breakdown {
        display: grid;
        gap: 10px;
        margin-top: 14px;
    }

    .breakdown-row {
        display: grid;
        grid-template-columns: minmax(120px, 1fr) 1.4fr 54px;
        gap: 10px;
        align-items: center;
    }

    .breakdown-bar { height: 10px; }
    .breakdown-val { color: var(--ink); font-weight: 700; text-align: right; }
    .risk-matrix { margin-top: 14px; }

    .lens-stack {
        display: grid;
        gap: 12px;
        margin-top: 12px;
    }

    .lens-stack > .gr-accordion,
    .lens-stack > [data-testid="accordion"] {
        position: relative;
        background: linear-gradient(180deg, rgba(33, 33, 34, 0.96) 0%, rgba(22, 22, 23, 0.96) 100%) !important;
        border: 1px solid rgba(86, 74, 58, 0.78) !important;
        box-shadow: inset 0 1px 0 rgba(255,255,255,0.02), 0 8px 24px rgba(0,0,0,0.22) !important;
    }

    .lens-stack > .gr-accordion::before,
    .lens-stack > [data-testid="accordion"]::before {
        content: "";
        position: absolute;
        inset: 0 auto 0 0;
        width: 3px;
        background: linear-gradient(180deg, rgba(196,164,109,0.95) 0%, rgba(135,113,84,0.9) 100%);
        pointer-events: none;
    }

    .layer-card {
        padding: 10px 18px 16px;
        background: var(--panel-2) !important;
        border: none !important;
        border-radius: 0 0 var(--radius-base) var(--radius-base);
    }

    .layer-card-head {
        margin-bottom: 14px;
        padding-bottom: 12px;
        border-bottom: 1px solid var(--line);
    }

    .layer-card-body,
    .gradio-container .gradio-markdown,
    .gradio-container .prose,
    .gradio-container .gradio-markdown p,
    .gradio-container .gradio-markdown li,
    .gradio-container .gradio-markdown strong {
        color: var(--ink) !important;
        background: transparent !important;
    }

    .layer-kv {
        display: grid;
        grid-template-columns: minmax(150px, 220px) 1fr;
        gap: 14px;
        align-items: start;
        padding: 14px 0;
        border-bottom: 1px solid rgba(196, 164, 109, 0.12);
    }

    .layer-kv:last-child {
        border-bottom: none;
        padding-bottom: 0;
    }

    .layer-kv span {
        color: var(--ink);
        font-size: 0.98rem;
        line-height: 1.6;
        font-weight: 600;
    }

    .layer-note-emphasis {
        margin-top: 14px;
        padding: 12px 14px;
        background: rgba(196, 164, 109, 0.08);
        border: 1px solid rgba(196, 164, 109, 0.2);
        border-radius: var(--radius-sm);
    }

    .action-list {
        display: grid;
        gap: 10px;
        margin: 0;
        padding-left: 18px;
        color: var(--ink);
    }

    .action-list li { line-height: 1.65; }

    .layer-blue,
    .layer-green,
    .layer-orange,
    .layer-purple,
    .layer-red {
        border-left: 2px solid var(--accent) !important;
    }

    .download-box {
        margin-top: 14px;
        padding: 14px;
        background: var(--panel-2) !important;
        border: 1px solid var(--line) !important;
        border-radius: var(--radius-base);
    }

    .soft-footer {
        padding: 18px 0 6px;
        margin-top: 18px;
        background: transparent !important;
        border: none !important;
        box-shadow: none !important;
    }

    #analyze-btn {
        min-height: 58px;
        background: var(--accent) !important;
        border: 1px solid var(--accent) !important;
        border-radius: var(--radius-base) !important;
        color: #090909 !important;
        font-size: 0.98rem !important;
        font-weight: 800 !important;
        letter-spacing: 0.14em !important;
        text-transform: uppercase !important;
        box-shadow: none !important;
        transition: transform 140ms ease, background 140ms ease, border-color 140ms ease !important;
    }

    #analyze-btn:hover {
        background: #d3b382 !important;
        border-color: #d3b382 !important;
        transform: translateY(-1px) !important;
    }

    #analyze-btn:active { transform: translateY(0) !important; }

    .gradio-container .block,
    .gradio-container .gr-box,
    .gradio-container .gr-group,
    .gradio-container .gr-form,
    .gradio-container .gr-panel,
    .gradio-container .gap-4,
    .gradio-container .row,
    .gradio-container [data-testid="block"] {
        background: transparent !important;
        border: none !important;
        box-shadow: none !important;
        color: var(--ink) !important;
    }

    .gradio-container .gr-accordion {
        background: var(--panel-2) !important;
        border: 1px solid var(--line) !important;
        border-radius: var(--radius-base) !important;
        box-shadow: none !important;
        overflow: hidden;
    }

    .gradio-container .gr-accordion > button {
        min-height: 70px;
        padding: 0 22px 0 26px !important;
        background: transparent !important;
        border: none !important;
        color: var(--ink) !important;
        font-family: 'Manrope', sans-serif !important;
        font-size: 1rem !important;
        font-weight: 800 !important;
        letter-spacing: 0.04em !important;
        text-transform: uppercase !important;
        position: relative;
    }

    .gradio-container .gr-accordion > button::after {
        content: "";
        position: absolute;
        left: 22px;
        right: 22px;
        bottom: 0;
        border-bottom: 1px solid rgba(196, 164, 109, 0.12);
    }

    .gradio-container .gr-accordion > button:hover {
        color: #fff7e8 !important;
        background: rgba(196, 164, 109, 0.04) !important;
    }

    .gradio-container .gr-accordion.open > button,
    .gradio-container .gr-accordion[open] > button {
        color: #fff7e8 !important;
    }

    .gradio-container textarea,
    .gradio-container input[type="text"],
    .gradio-container input[type="number"],
    .gradio-container .scroll-hide {
        background: var(--panel-2) !important;
        border: 1px solid var(--line) !important;
        border-radius: var(--radius-sm) !important;
        color: var(--ink) !important;
        box-shadow: none !important;
    }

    .gradio-container textarea:focus,
    .gradio-container input[type="text"]:focus,
    .gradio-container input[type="number"]:focus {
        border-color: var(--accent) !important;
        box-shadow: 0 0 0 1px var(--accent) !important;
    }

    .gradio-container .wrap,
    .gradio-container .image-container,
    .gradio-container .upload-container,
    .gradio-container .empty,
    .gradio-container .file-preview,
    .gradio-container .file-wrap,
    .gradio-container [data-testid="image"],
    .gradio-container .gr-image,
    .gradio-container .gr-file {
        background: var(--panel-2) !important;
        border: 1px dashed var(--line-strong) !important;
        border-radius: var(--radius-base) !important;
        box-shadow: none !important;
        color: var(--ink-soft) !important;
    }

    .gradio-container .wrap:hover,
    .gradio-container .image-container:hover,
    .gradio-container .upload-container:hover,
    .gradio-container .empty:hover,
    .gradio-container .file-preview:hover,
    .gradio-container .file-wrap:hover,
    .gradio-container [data-testid="image"]:hover,
    .gradio-container .gr-image:hover,
    .gradio-container .gr-file:hover {
        border-color: var(--accent) !important;
    }

    .gradio-container .gradio-markdown h1,
    .gradio-container .gradio-markdown h2,
    .gradio-container .gradio-markdown h3 {
        color: var(--ink) !important;
        font-family: 'Cormorant Garamond', serif !important;
        font-weight: 600;
    }

    .gradio-container .gradio-markdown blockquote {
        margin-left: 0;
        padding-left: 14px;
        border-left: 2px solid var(--accent);
        color: var(--ink-soft) !important;
        font-style: italic;
    }

    .gradio-container .gradio-markdown code {
        padding: 2px 6px;
        border: 1px solid var(--line);
        border-radius: 6px;
        background: #090909 !important;
        color: var(--ink-soft) !important;
        font-weight: 700;
    }

    .gradio-container svg {
        color: var(--muted) !important;
        stroke: var(--muted) !important;
    }

    .gradio-container button:not(#analyze-btn) {
        background: var(--panel-2) !important;
        border: 1px solid var(--line) !important;
        border-radius: var(--radius-sm) !important;
        color: var(--ink) !important;
        font-weight: 700 !important;
        transition: border-color 140ms ease, color 140ms ease !important;
    }

    .gradio-container button:not(#analyze-btn):hover {
        border-color: var(--accent) !important;
        color: var(--ink) !important;
    }

    .gradio-container button:focus-visible,
    .gradio-container input:focus-visible,
    .gradio-container textarea:focus-visible {
        outline: none !important;
        box-shadow: 0 0 0 1px var(--accent) !important;
    }

    ::-webkit-scrollbar { width: 8px; height: 8px; }
    ::-webkit-scrollbar-track { background: var(--bg); }
    ::-webkit-scrollbar-thumb {
        background: #2b261f;
        border-radius: 999px;
    }
    ::-webkit-scrollbar-thumb:hover { background: #3b3329; }

    @media (max-width: 1100px) {
        .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        .submission-card {
            position: static;
        }
    }

    @media (max-width: 900px) {
        .gradio-container { padding: 24px 16px 48px !important; }
        .soft-status {
            min-height: auto;
        }
        .masthead {
            grid-template-columns: 1fr;
        }
        .masthead-side {
            padding-left: 0;
            padding-top: 16px;
            border-left: none;
            border-top: 1px solid rgba(196, 164, 109, 0.16);
        }
    }

    @media (max-width: 640px) {
        .summary-grid { grid-template-columns: 1fr; }
        .section-title { font-size: 1.75rem; }
        .summary-title,
        .layer-card-title { font-size: 1.7rem; }
        .layer-kv,
        .breakdown-row { grid-template-columns: 1fr; }
        .submission-meta { grid-template-columns: 1fr; }
        .masthead-title { font-size: 3rem; }
    }
    """

    with gr.Blocks(title="Luxury Truth Lens") as demo:
        gr.HTML(f"<style>{css}</style>")
        gr.HTML(
            """
            <section class="hero-shell">
              <div class="hero-kicker">Image Review Atelier</div>
              <h1 class="hero-title">Luxury Truth Lens</h1>
              <p class="hero-copy">
                A restrained review surface for luxury image triage.
                Submit a single frame and read five structured lenses:
                origin, identity, confidence, provenance, and recommended next action.
              </p>
              <div class="hero-grid">
                <div class="hero-pill"><span class="hero-pill-label">Accepted Image</span>JPG, PNG, or WebP, up to 10 MB</div>
                <div class="hero-pill"><span class="hero-pill-label">HF Token</span>Optional—speeds model downloads significantly</div>
                <div class="hero-pill"><span class="hero-pill-label">Use Case</span>Screen risk quickly, then escalate to specialist review</div>
              </div>
            </section>
            """
        )

        with gr.Row(elem_classes=["masthead-row"]):
            with gr.Column(scale=18):
                gr.HTML(
                    """
                    <section class="masthead">
                      <div>
                        <div class="masthead-mark">Luxury image review</div>
                        <h1 class="masthead-title">Luxury Truth Lens</h1>
                        <p class="masthead-copy">
                          Review one image at a time with a denser two-panel workspace built for fast visual triage,
                          provenance checks, and cleaner decision support.
                        </p>
                      </div>
                      <div class="masthead-side">
                        <div>
                          <div class="masthead-side-label">Mode</div>
                          <div class="masthead-side-value">Five-lens review</div>
                        </div>
                        <div>
                          <div class="masthead-side-label">Canvas</div>
                          <div class="masthead-side-value">Editorial workspace</div>
                        </div>
                      </div>
                    </section>
                    """
                )
            with gr.Column(scale=5):
                status = gr.Markdown("Status: standing by.", elem_classes=["soft-status"])

        with gr.Row(equal_height=False, elem_classes=["workspace-row"]):
            with gr.Column(scale=9, min_width=440):
                with gr.Group(elem_classes=["soft-card", "submission-card"]):
                    gr.Markdown("## Submission", elem_classes=["section-title"])
                    gr.Markdown(
                        "Upload a frame or choose a sample, then run a structured review.",
                        elem_classes=["section-copy"],
                    )
                    with gr.Group(elem_classes=["well"]):
                        img_in = gr.Image(
                            label="Luxury item image",
                            type="numpy",
                            height=560,
                            sources=["upload"],
                        )
                    gr.HTML(
                        """
                        <div class="submission-meta">
                          <div class="meta-chip"><strong>Accepted image</strong>JPG, PNG, WebP up to 10 MB</div>
                          <div class="meta-chip"><strong>Use</strong>Screen quickly, then escalate to specialist review</div>
                        </div>
                        """
                    )
                    examples = _example_paths()
                    if examples:
                        gr.Examples(
                            examples=examples,
                            inputs=[img_in],
                            label="Examples",
                        )
                    btn = gr.Button("Run Review", elem_id="analyze-btn", variant="primary")

            with gr.Column(scale=14, min_width=620):
                with gr.Group(elem_classes=["soft-card"]):
                    gr.Markdown("## Review", elem_classes=["section-title"])
                    gr.Markdown(
                        "Read the top-line judgment first, then move through the five supporting lenses.",
                        elem_classes=["section-copy"],
                    )
                    with gr.Group(elem_classes=["summary-box"]):
                        summary = gr.HTML("<div class='summary-panel'><div class='summary-title'>Submit an image to generate a review.</div></div>")
                    with gr.Group(elem_classes=["lens-stack"]):
                        with gr.Accordion("Lens I  Origin", open=True):
                            with gr.Group(elem_classes=["layer-card"]):
                                out1 = gr.Markdown()
                                gr.Markdown("*How the image appears to have been produced, and how certain that read is.*", elem_classes=["layer-note"])
                        with gr.Accordion("Lens II  Identity", open=True):
                            with gr.Group(elem_classes=["layer-card"]):
                                out2 = gr.Markdown()
                                gr.Markdown("*Brand, category, caption, and alternate interpretations from the model.*", elem_classes=["layer-note"])
                        with gr.Accordion("Lens III  Confidence", open=True):
                            with gr.Group(elem_classes=["layer-card"]):
                                out3 = gr.Markdown()
                                gr.Markdown("*Visual confidence score and supporting signal. Not a professional authentication result.*", elem_classes=["layer-note"])
                        with gr.Accordion("Lens IV  Provenance", open=True):
                            with gr.Group(elem_classes=["layer-card"]):
                                out4 = gr.Markdown()
                                gr.Markdown("*Reference lookups against known flagged entries and stored provenance notes.*", elem_classes=["layer-note"])
                        with gr.Accordion("Lens V  Actions", open=True):
                            with gr.Group(elem_classes=["layer-card"]):
                                out5 = gr.Markdown()
                                gr.Markdown("*Recommended follow-up actions shaped by the full review.*", elem_classes=["layer-note"])
                    meta = gr.Markdown(
                        f"**Token status:** `{_hf_token_status()}`\n\n**Disclaimer:** {TOP_DISCLAIMER}",
                        elem_classes=["soft-footer"],
                    )
                    with gr.Group(elem_classes=["download-box"]):
                        json_file = gr.File(label="Download JSON review")
                        pdf_button = gr.Button("Export PDF Review")
                        pdf_file = gr.File(label="Download PDF review")
                        recent = gr.Dropdown(choices=[], label="Recent reviews", interactive=True)
                        recent_summary = gr.HTML("", visible=True)

        btn.click(
            fn=run_analysis,
            inputs=[img_in],
            outputs=[summary, out1, out2, out3, out4, out5, meta, status, json_file, recent, recent_summary],
            api_name="analyze",
            show_progress="full",
        )

        def _export_pdf(path: str):
            return export_json_to_pdf(path)

        pdf_button.click(fn=_export_pdf, inputs=[json_file], outputs=[pdf_file])

        def _load_recent(selected_label: str):
            if not selected_label:
                return ""
            # Find matching history entry by label
            hist = list(HISTORY)
            target = None
            for item in hist:
                label = f"{item.get('brand') or 'unknown'} - {item.get('score')}/100"
                if label == selected_label or selected_label.startswith(label):
                    target = item
                    break
            if target is None:
                return ""
            try:
                with open(target["path"], "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception as exc:
                return f"<div class='layer-note'>Failed to load: {html.escape(str(exc))}</div>"
            l2 = data.get("layer2", {})
            l3 = data.get("layer3", {})
            l4 = data.get("layer4", {})
            summary_html = _summary_html(data.get("layer5", {}).get("severity","info"), l2.get("brand","-"), l2.get("category","-"), l3.get("confidence_score",0), l4.get("provenance_status","-"), data.get("layer5",{}).get("actions",[]))
            return summary_html

        recent.change(fn=_load_recent, inputs=[recent], outputs=[recent_summary])

    return demo


if __name__ == "__main__":
    build_ui().queue(default_concurrency_limit=2).launch()
