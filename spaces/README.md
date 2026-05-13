---
title: Luxury Authenticator
emoji: 💎
colorFrom: gray
colorTo: yellow
sdk: gradio
sdk_version: 6.14.0
python_version: 3.11
app_file: app.py
pinned: false
---

# Luxury Authenticator

**One image. Any source. Structured signals.**

A 5-layer AI pipeline (research demo) that analyses a luxury-related image and returns a structured report: image source type, item identification, a CLIP-based confidence signal, a perceptual-hash provenance check against a small demo database, and rule-based recommended actions.

## Architecture

| Layer | Purpose | Model / method |
|-------|---------|----------------|
| 1 — Image type | Real photo / AI / Screenshot / Render | CLIP zero-shot (`openai/clip-vit-base-patch32`) |
| 2 — Object ID | Brand, category, caption | BLIP-base + CLIP (`Salesforce/blip-image-captioning-base`) |
| 3 — Confidence signal | Visual similarity vs authentic vs replica wording | CLIP zero-shot (same CLIP) |
| 4 — Provenance | Near-duplicate check vs demo scam list | `imagehash` pHash + `data/scam_database.csv` |
| 5 — Actions | Short recommendations | Rule engine (no LLM) |

## Honest limitations

- The confidence signal (Layer 3) uses CLIP visual similarity, not professional authentication.
- The provenance database contains a small set of manually seeded demo entries (hashes from `data/flagged/`).
- Do not use for purchase decisions over INR 50,000 without professional verification.

## Local run

```bash
pip install -r requirements.txt
python app.py
```

First request downloads PyTorch and model weights (expect several minutes and ~2 GB RAM peak on CPU).

## Rebuild scam CSV

After adding PNG/JPG files under `data/flagged/`:

```bash
python data/seed_phashes.py
```

## Demo assets

Example images live in `examples/`. Flagged images used only for hashing live in `data/flagged/`. Uploading `examples/fb_listing_screenshot.png` should match the duplicate entry in the demo database and show **Flagged** in Layer 4.

## Timing (reference)

End-to-end inference target is under ~25 seconds per image on Hugging Face Spaces CPU after models are loaded; cold start adds model download and load time on the first request.

## Smoke tests

- Layer 4 only (no PyTorch): `set PYTHONPATH=%CD%` then `python scripts/smoke_layer4.py` (Windows) or `PYTHONPATH=. python scripts/smoke_layer4.py` (Unix).
- Full pipeline (requires `pip install -r requirements.txt`): `python scripts/smoke_analyse.py` from the repo root (prints timing and a short summary for `examples/chanel_bag.png`).

## V2 ideas

GradCAM / heatmaps, TinEye-style API for provenance, larger brand lists, calibrated thresholds, optional LLM action layer.

## Built by

IT portfolio project — Luxury Truth Lens PRD v2.0 (Buildable Edition).
