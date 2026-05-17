# Adversarial Framing Engine

Small FastAPI app that generates non-obvious, validity-scored approaches to a problem by querying an LLM pipeline.

## Overview
- Backend: `app.py` (FastAPI) — POST `/analyze` streams Server-Sent Events for a 5-step pipeline.
- Frontend: `static/index.html` — simple UI that posts to `/analyze` and renders results.

## Model configuration
The server prefers a local Ollama runtime when `OLLAMA_URL` is set (e.g. `http://localhost:11434`). If `OLLAMA_URL` is provided, the app will send requests to that endpoint. Otherwise, it falls back to Anthropic and requires `ANTHROPIC_API_KEY`.

Environment variables:
- `OLLAMA_URL` — optional. Example: `http://localhost:11434` (preferred for local runs).
- `OLLAMA_MODEL` — optional. Model name to pass to Ollama (default: `llama2`).
- `ANTHROPIC_API_KEY` — optional fallback if `OLLAMA_URL` is not set.

## Install
Windows PowerShell example:

```powershell
python -m pip install -r requirements.txt
```

## Run (development)

Windows PowerShell example (uses Ollama if available):

```powershell
#$env:OLLAMA_URL = "http://localhost:11434"
uvicorn app:app --reload
```

If you want to use Anthropic instead:

```powershell
#$env:ANTHROPIC_API_KEY = "your_key_here"
uvicorn app:app --reload
```

## Notes
- The app attempts to parse JSON objects from model responses. Ensure your model outputs valid JSON objects when used with this pipeline.
- If you're running Ollama locally, confirm its HTTP API endpoint and update `OLLAMA_URL` accordingly.