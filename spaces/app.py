import json
import os
import tempfile
import traceback
from pathlib import Path

import gradio as gr

from pipeline.orchestrator import analyse

TOP_DISCLAIMER = (
    "Research demo. Not professional authentication. "
    "Do not use for high-value purchase decisions."
)


def _example_paths():
    base = Path(__file__).resolve().parent / "examples"
    if not base.is_dir():
        return []
    out = []
    for name in sorted(os.listdir(base)):
        p = base / name
        if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            out.append(str(p))
    return out


def _severity_badge(severity: str) -> str:
    sev = (severity or "info").lower()
    if sev not in {"info", "caution", "warning", "critical"}:
        sev = "info"
    return f"<span class='sev sev-{sev}'>{sev.upper()}</span>"


def _headline_for(severity: str) -> str:
    sev = (severity or "info").lower()
    return {
        "info": "Looks consistent (demo signal)",
        "caution": "Proceed with caution",
        "warning": "High risk / likely mismatch",
        "critical": "Do not engage / likely scam",
    }.get(sev, "Proceed with caution")


def run_analysis(image, status_md, progress=gr.Progress(track_tqdm=False)):
    if image is None:
        return (
            "**Error:** Please upload a JPG, PNG, or WebP image (max 10 MB).",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            None,
        )

    try:
        status_md.update(value="Status: running analysis...")
        result = analyse(image, progress=progress)
    except ValueError as e:
        return (f"**Error:** {e}", "", "", "", "", "", "", "", None)
    except Exception:
        tb = traceback.format_exc()
        return (
            "**Error:** Something went wrong.\n```\n" + tb + "\n```",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            None,
        )

    l1 = result["layer1"]
    l2 = result["layer2"]
    l3 = result["layer3"]
    l4 = result["layer4"]
    l5 = result["layer5"]

    severity = l5.get("severity", "info")
    badge = _severity_badge(severity)
    headline = _headline_for(severity)

    actions = l5.get("actions", [])
    actions_md = "\n".join([f"- {a}" for a in actions]) if actions else "- (none)"

    summary_card = (
        f"<div class='card'>"
        f"<div class='card-top'>"
        f"<div class='title'>Luxury Truth Lens</div>"
        f"<div class='right'>{badge}</div>"
        f"</div>"
        f"<div class='headline'>{headline}</div>"
        f"<div class='meta'>Brand: <b>{l2['brand']}</b> | Category: <b>{l2['category']}</b></div>"
        f"<div class='meta'>Layer 3 score: <b>{l3['confidence_score']}/100</b> ({l3['signal_label']})</div>"
        f"<div class='meta'>Provenance: <b>{l4['provenance_status']}</b> (DB: {l4.get('db_entry_count', 0)})</div>"
        f"<hr/>"
        f"<div class='actions'><b>Recommended actions</b>\n{actions_md}</div>"
        f"</div>"
    )

    u = " (uncertain)" if l1.get("uncertain") else ""
    md1 = (
        f"**Source type:** {l1['source_type']}{u}\n\n"
        f"**Model confidence:** {l1['confidence']*100:.1f}%"
    )

    alts = ", ".join(l2.get("alt_guesses") or []) or "—"
    md2 = (
        f"**Caption:** {l2['caption']}\n\n"
        f"**Brand:** {l2['brand']} | **Category:** {l2['category']}\n\n"
        f"**Brand confidence:** {l2['confidence']*100:.1f}%\n\n"
        f"**Alt guesses:** {alts}"
    )

    md3 = (
        f"**Score:** {l3['confidence_score']}/100\n\n"
        f"**Signal:** {l3['signal_label']}\n\n"
        f"> {l3['disclaimer']}"
    )

    lines4 = [
        f"**Status:** {l4['provenance_status']}",
        f"**Demo database entries:** {l4.get('db_entry_count', 0)}",
    ]
    if l4.get("match_source"):
        lines4.append(f"**Match source:** {l4['match_source']}")
    if l4.get("match_date"):
        lines4.append(f"**Reported date:** {l4['match_date']}")
    if l4.get("note"):
        lines4.append(f"**Note:** {l4['note']}")
    md4 = "\n\n".join(lines4)

    md5 = f"{badge}\n\n" + actions_md

    disclaimer_footer = "**Disclaimer:** " + result.get("global_disclaimer", "")

    tmpdir = Path(tempfile.gettempdir())
    json_path = tmpdir / "luxury_truth_lens_report.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    status_text = "Status: complete."

    return (
        summary_card,
        md1,
        md2,
        md3,
        md4,
        md5,
        disclaimer_footer,
        status_text,
        str(json_path),
    )


def build_ui():
    theme = gr.themes.Soft(
        primary_hue=gr.themes.Color(
            c50="#FFF8E1",
            c100="#FFECB3",
            c200="#FFE082",
            c300="#FFD54F",
            c400="#FFCA28",
            c500="#FFC107",
            c600="#FFB300",
            c700="#FFA000",
            c800="#FF8F00",
            c900="#FF6F00",
            c950="#E65100",
        ),
        neutral_hue=gr.themes.colors.zinc,
        font=[gr.themes.GoogleFont("Source Sans 3"), "sans-serif"],
    )

    css = """
    .topbox { border-left: 4px solid #c9a227; padding-left: 12px; margin: 8px 0 14px; }
    .card { border: 1px solid rgba(255,255,255,0.08); border-radius: 14px; padding: 14px; background: rgba(0,0,0,0.22); }
    .card-top { display:flex; justify-content:space-between; align-items:center; }
    .title { font-weight: 700; font-size: 16px; }
    .headline { font-weight: 700; font-size: 18px; margin-top: 8px; }
    .meta { opacity: 0.92; margin-top: 6px; }
    .actions { margin-top: 10px; white-space: pre-wrap; }
    .sev { font-size: 12px; padding: 4px 10px; border-radius: 999px; font-weight: 700; }
    .sev-info { background: rgba(59,130,246,0.18); border: 1px solid rgba(59,130,246,0.38); }
    .sev-caution { background: rgba(245,158,11,0.18); border: 1px solid rgba(245,158,11,0.38); }
    .sev-warning { background: rgba(239,68,68,0.18); border: 1px solid rgba(239,68,68,0.38); }
    .sev-critical { background: rgba(220,38,38,0.22); border: 1px solid rgba(220,38,38,0.55); }
    """

    with gr.Blocks(theme=theme, title="Luxury Truth Lens (Research Demo)", css=css) as demo:
        gr.Markdown(
            "# Luxury Truth Lens\n### One image. Any source. Structured signals.\n\n"
            + f"<div class='topbox'>{TOP_DISCLAIMER}</div>"
        )

        status = gr.Markdown("Status: idle")

        with gr.Row():
            with gr.Column(scale=1):
                img_in = gr.Image(label="Upload image", type="numpy", height=400)
                gr.Markdown("*JPG / PNG / WebP - max 10 MB*")
                ex = _example_paths()
                if ex:
                    gr.Examples(examples=[[p] for p in ex], inputs=[img_in], label="Examples")

            with gr.Column(scale=1):
                summary_html = gr.HTML()
                with gr.Accordion("Layer 1 - Source type", open=False):
                    out1 = gr.Markdown()
                with gr.Accordion("Layer 2 - Object ID", open=False):
                    out2 = gr.Markdown()
                with gr.Accordion("Layer 3 - Confidence signal", open=False):
                    out3 = gr.Markdown()
                with gr.Accordion("Layer 4 - Provenance", open=False):
                    out4 = gr.Markdown()
                with gr.Accordion("Layer 5 - Actions", open=False):
                    out5 = gr.Markdown()

                foot = gr.Markdown()
                json_file = gr.File(label="Download JSON report")

        btn = gr.Button("Analyse", variant="primary")
        btn.click(
            fn=run_analysis,
            inputs=[img_in, status],
            outputs=[summary_html, out1, out2, out3, out4, out5, foot, status, json_file],
        )

    return demo


if __name__ == "__main__":
    build_ui().launch()