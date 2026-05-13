# Adversarial Framing Engine - Hybrid Pipeline (Hugging Face + Ollama)
import os, json, re, glob, asyncio, secrets, httpx, openai, anthropic
import json_repair
# Load .env file so HF_TOKEN etc. are available without manual env setup
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)
except ImportError:
    pass  # python-dotenv not installed; fall back to system env vars

from typing import Any
from functools import partial
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import auth
import patterns
from datetime import datetime

app = FastAPI(title="Adversarial Framing Engine")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BASE_DIR = os.path.dirname(__file__)
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.path.join(BASE_DIR, "data")
ANALYSES_DIR = os.path.join(DATA_DIR, "analyses")


# Domain intelligence layer: per-domain lens framings
DOMAIN_CONFIGS = {
    "general": {
        "first_principles": "strip all assumptions and rebuild from zero — focus on fundamental constraints and primitives.",
        "inversion": "solve the opposite problem and see what it reveals — think about how to make the goal fail, then reverse.",
        "analogical": "find a solved version in an unrelated domain and apply that solution — use cross-domain metaphors and analogies.",
        "constraint_removal": "remove the biggest assumed constraint (budget/team/time) — what becomes possible without it?",
        "second_order": "consider second-order effects: what new problems or dynamics arise from the obvious solution?",
    },
    "startup": {
        "first_principles": "what is the absolute core value the startup creates? reduce to that and rebuild around it.",
        "inversion": "what action would make the startup fail fastest — now invert its lessons into proactive moves.",
        "analogical": "find how a company in a completely different industry solved rapid growth or distribution and apply that to customer acquisition.",
        "constraint_removal": "assume you have 100x your current budget/team — what changes in product, go-to-market, and hiring?",
        "second_order": "if everyone copied your growth hack, what would break in acquisition channels or unit economics?",
    },
    "product": {
        "first_principles": "reduce the product to the core user job-to-be-done and rebuild the experience around that.",
        "inversion": "what product change would guarantee users stop using it — then reverse-engineer improvements.",
        "analogical": "find a product in another category that solved retention/engagement and translate its mechanics.",
        "constraint_removal": "remove assumed platform or tech limits — what richer UX or integrations become feasible?",
        "second_order": "if this product decision scales, what secondary behaviors or platform shifts occur?",
    },
    "engineering": {
        "first_principles": "strip architecture to required invariants (consistency, latency, throughput) and redesign from those primitives.",
        "inversion": "what engineering choice would maximally increase bugs or outages — invert it to harden the system.",
        "analogical": "find how a different engineering-heavy domain (e.g. aerospace) enforces safety/robustness and borrow patterns.",
        "constraint_removal": "assume unlimited compute or storage — how does design simplify or change?",
        "second_order": "if this optimization were applied everywhere, what systemic fragility appears?",
    },
    "finance": {
        "first_principles": "identify the fundamental cash flows, constraints, and incentives; rebuild decisions around risk-adjusted returns.",
        "inversion": "what position would guarantee you lose money here — now reverse it into a protective strategy.",
        "analogical": "find analogous market structures in other asset classes and apply their hedging or arbitrage tactics.",
        "constraint_removal": "assume capital is unlimited — what new strategies, leverage, or instruments open up?",
        "second_order": "if everyone adopted this strategy, what market dynamic breaks or feedback loop emerges?",
    },
    "research": {
        "first_principles": "isolate the scientific axioms and testable hypotheses; rebuild experiments from minimal assumptions.",
        "inversion": "what experiment would most strongly disconfirm your hypothesis — design that and then flip insights into robust methods.",
        "analogical": "find breakthroughs in unrelated sciences and adapt their experimental or measurement techniques.",
        "constraint_removal": "remove resource or measurement limitations — what experiments become possible?",
        "second_order": "if this finding were accepted, what downstream research programs or ethical concerns arise?",
    },
}


def get_clients():
    clients = {}
    # Hugging Face (Smart)
    hf_token = os.environ.get("HF_TOKEN")
    if hf_token:
        print(f"[get_clients] using Hugging Face router (HF_TOKEN present)", flush=True)
        client = openai.OpenAI(api_key=hf_token, base_url="https://router.huggingface.co/v1")
        default_model = "meta-llama/Llama-3.1-8B-Instruct:nscale"
        clients["hf"] = {"type": "hf_router", "client": client, "model": os.environ.get("HF_MODEL", default_model)}

    # Ollama (Cheap/Local)
    ollama_url = os.environ.get("OLLAMA_URL") or os.environ.get("OLLAMA_HOST")
    if ollama_url:
        print(f"[get_clients] using ollama at {ollama_url}", flush=True)
        clients["ollama"] = {"type": "ollama", "url": ollama_url.rstrip('/'), "model": os.environ.get("OLLAMA_MODEL", "llama2")}

    # Anthropic
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        clients["anthropic"] = anthropic.AsyncAnthropic(api_key=key)
        
    if not clients:
        raise ValueError("No models configured. Set HF_TOKEN, OLLAMA_URL, or ANTHROPIC_API_KEY.")
    return clients


class AnalyzeRequest(BaseModel):
    problem: str
    domain: str = "general"


def robust_json_loads(text: str):
    text = str(text)
    # Strip Qwen3 / DeepSeek thinking blocks: <think>...</think>
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()
    # Strip markdown fences that some models add around JSON.
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text).strip()
    
    try:
        # Use json_repair to handle malformed LLM JSON (trailing commas, truncation, etc)
        parsed = json_repair.loads(text)
        if not isinstance(parsed, (dict, list)):
             raise ValueError("Parsed JSON is not an object or array.")
        return parsed
    except Exception as e:
        print(f"[robust_json_loads] json_repair failed on text[:200]={text[:200]}: {e}")
        raise ValueError(f"Failed to parse JSON: {e}")


def parse_ollama_ndjson_text(text: str):
    """Parse Ollama line-delimited JSON stream text into a final payload.

    Ollama /api/generate can return multiple JSON objects separated by newlines,
    where each line has partial `response` tokens.
    """
    chunks = []
    last_obj = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        last_obj = obj
        piece = obj.get("response") or obj.get("text") or ""
        if piece:
            chunks.append(piece)

    combined = "".join(chunks).strip()
    if combined:
        return robust_json_loads(combined)
    if isinstance(last_obj, dict):
        return last_obj
    raise ValueError("Failed to parse Ollama streamed response.")


def normalize_llm_output(obj: Any, expected_field: str | None = None, expected_item_keys: list | None = None) -> dict:
    """Coerce LLM outputs into a dict with a predictable shape.

    - If `obj` is a list of dicts and items match `expected_item_keys`, return {expected_field: obj}.
    - If `obj` is a single-item list, return that item if it's a dict.
    - If `obj` is a list of scalars, join them into a string under `expected_field` or 'consensus'.
    - If `obj` is not a dict, wrap it into {expected_field or 'consensus': str(obj)}.
    """
    if isinstance(obj, list):
        if len(obj) == 0:
            return {expected_field or "consensus": ""}

        # List of dicts
        if all(isinstance(x, dict) for x in obj):
            if expected_field and expected_item_keys:
                if any(all(k in x for k in expected_item_keys) for x in obj):
                    return {expected_field: obj}
            if len(obj) == 1:
                return obj[0]
            if expected_field:
                return {expected_field: obj}
            return obj[0]

        # List of scalars -> join into a string
        try:
            return {expected_field or "consensus": " ".join(str(x) for x in obj)}
        except Exception:
            return {expected_field or "consensus": str(obj)}

    if not isinstance(obj, dict):
        return {expected_field or "consensus": str(obj)}

    return obj


async def call_llm(client: Any, system: str, user: str) -> dict:
    prompt = system + "\n\n" + user
    print(f"[call_llm] invoked; client summary: {str(client)[:400]}", flush=True)
    # Ollama path (local HTTP endpoint)
    if isinstance(client, dict) and client.get("type") == "ollama":
        url = client.get("url")
        model = client.get("model", "llama2")
        async with httpx.AsyncClient() as ac:
            # try common Ollama endpoints
            tries = [f"{url}/api/generate", f"{url}/generate", url]
            for endpoint in tries:
                try:
                    r = await ac.post(
                        endpoint,
                        json={"model": model, "prompt": prompt, "max_tokens": 1500, "stream": False},
                        timeout=45.0,
                    )
                except Exception:
                    print(f"[ollama] request to {endpoint} raised exception")
                    continue
                if r.status_code != 200:
                    print(f"[ollama] endpoint {endpoint} returned status {r.status_code}: {r.text[:400]}")
                    continue
                try:
                    j = r.json()
                except Exception:
                    text = r.text
                    print(f"[ollama] failed to decode JSON from {endpoint}; text[:800]: {text[:800]}")
                    return parse_ollama_ndjson_text(text)
                # common response fields
                text = j.get("text") or j.get("response") or j.get("output") or j.get("result") or ""
                if not text:
                    # sometimes there's a generations list
                    gens = j.get("generations") or j.get("generated")
                    if isinstance(gens, list):
                        text = "".join([g.get("text", "") if isinstance(g, dict) else str(g) for g in gens])
                if not text:
                    if isinstance(j, dict): return j
                    continue
                return robust_json_loads(text)
        raise ValueError("Ollama request failed or returned unexpected format.")

    # Hugging Face router path (via OpenAI-compatible client)
    if isinstance(client, dict) and client.get("type") == "hf_router":
        hf_client = client.get("client")
        model = client.get("model")

        def _call_hf(msgs):
            return hf_client.chat.completions.create(
                model=model,
                messages=msgs,
                max_tokens=4096,
            )

        def _extract_text(completion):
            msg = completion.choices[0].message if hasattr(completion.choices[0], 'message') else completion.choices[0]
            return getattr(msg, 'content', None) or (msg.get('content') if isinstance(msg, dict) else str(msg)) or ""

        try:
            print(f"[hf_router] sending chat completion to model {model}", flush=True)
            json_hint = "\n\nRespond with ONLY a valid JSON object. No prose, no markdown, no explanation. Output must start with { and end with }."
            messages = [
                {"role": "system", "content": system + json_hint},
                {"role": "user",   "content": user},
            ]
            loop = asyncio.get_running_loop()
            completion = await loop.run_in_executor(None, lambda: _call_hf(messages))
            text = _extract_text(completion)
            print(f"[hf_router] raw response[:600]: {str(text)[:600]}", flush=True)

            try:
                return robust_json_loads(text)
            except Exception as parse_err:
                # Repair pass: ask the model to convert its own prose answer to JSON
                print(f"[hf_router] JSON parse failed ({parse_err}); attempting repair pass", flush=True)
                repair_messages = [
                    {"role": "system", "content": "You are a JSON formatter. Convert the user's text into the required JSON format exactly. Output ONLY the JSON object, nothing else."},
                    {"role": "user",   "content": f"Convert this text into a valid JSON object matching this schema: {system}\n\nText to convert:\n{text}"},
                ]
                repair_completion = await loop.run_in_executor(None, lambda: _call_hf(repair_messages))
                repair_text = _extract_text(repair_completion)
                print(f"[hf_router] repair response[:600]: {str(repair_text)[:600]}", flush=True)
                return robust_json_loads(repair_text)

        except Exception as e:
            print(f"[hf_router] exception: {e}", flush=True)
            raise

    # Default: Anthropic async client
    msg = await client.messages.create(
        model="claude-3-5-sonnet-20240620",
        max_tokens=1500,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in msg.content if hasattr(b, "text"))
    return robust_json_loads(text)


def composite_score(s: dict, c: dict) -> float:
    n  = s.get("novelty", 0.5)
    f  = s.get("feasibility", 0.5)
    r  = s.get("risk", 0.5)
    ei = s.get("expected_impact", 0.5)
    cd = c.get("confidence_drop", 0.2)
    return ((n * 0.25 + f * 0.35 + ei * 0.35) * (1 - r * 0.15)) * (1 - cd * 0.3)


def event(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def ensure_analysis_dirs():
    os.makedirs(ANALYSES_DIR, exist_ok=True)


def make_analysis_id() -> str:
    return datetime.utcnow().strftime("%Y%m%d%H%M%S") + secrets.token_hex(3)


def is_valid_analysis_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]+", value or ""))


def truncate_problem(problem: str, limit: int = 80) -> str:
    clean = " ".join((problem or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def read_json_file(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json_file(path: str, payload: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def analysis_summary(record: dict) -> dict:
    ranked = record.get("ranked") or []
    top = ranked[0] if ranked else {}
    return {
        "id": record.get("id"),
        "problem": truncate_problem(record.get("problem", "")),
        "domain": record.get("domain", "general"),
        "top_approach": top.get("lens"),
        "composite_score": top.get("composite"),
        "created_at": record.get("created_at"),
    }


def save_analysis(key: str, problem: str, result: dict) -> dict:
    ensure_analysis_dirs()
    analysis_id = make_analysis_id()
    key_hash = auth.key_hash(key)
    user_dir = os.path.join(ANALYSES_DIR, key_hash)
    os.makedirs(user_dir, exist_ok=True)

    record = {
        "id": analysis_id,
        "problem": problem,
        "domain": result.get("domain", "general"),
        "domain_config": result.get("domain_config", {}),
        "consensus": result.get("consensus", {}),
        "ranked": result.get("ranked", []),
        "created_at": utc_now_iso(),
    }
    write_json_file(os.path.join(user_dir, f"{analysis_id}.json"), record)
    try:
        patterns.record_analysis(key, problem, record["domain"], record["ranked"])
    except Exception as exc:
        print(f"[save_analysis] pattern record failed: {exc}", flush=True)
    return record


def list_analyses(key: str, page: int = 1, per_page: int = 20) -> dict:
    ensure_analysis_dirs()
    key_hash = auth.key_hash(key)
    user_dir = os.path.join(ANALYSES_DIR, key_hash)
    if not os.path.exists(user_dir):
        return {"items": [], "page": page, "per_page": per_page, "total": 0, "has_more": False}

    filenames = sorted(glob.glob(os.path.join(user_dir, "*.json")), reverse=True)
    total = len(filenames)
    start = max(0, (page - 1) * per_page)
    end = start + per_page
    items = [analysis_summary(read_json_file(path)) for path in filenames[start:end]]
    return {
        "items": items,
        "page": page,
        "per_page": per_page,
        "total": total,
        "has_more": end < total,
    }


def get_analysis_for_key(key: str, analysis_id: str):
    if not is_valid_analysis_id(analysis_id):
        return None
    key_hash = auth.key_hash(key)
    path = os.path.join(ANALYSES_DIR, key_hash, f"{analysis_id}.json")
    if not os.path.exists(path):
        return None
    return read_json_file(path)


def get_public_analysis(analysis_id: str):
    if not is_valid_analysis_id(analysis_id):
        return None
    matches = glob.glob(os.path.join(ANALYSES_DIR, "*", f"{analysis_id}.json"))
    if not matches:
        return None
    return read_json_file(matches[0])


def load_static_html(name: str) -> str:
    static_path = os.path.join(STATIC_DIR, name)
    repo_root_path = os.path.join(BASE_DIR, name)
    if os.path.exists(static_path):
        path = static_path
    elif os.path.exists(repo_root_path):
        path = repo_root_path
    else:
        raise HTTPException(status_code=404, detail=f"{name} not found")
    with open(path, encoding="utf-8") as f:
        return f.read()

async def keep_alive(coro):
    """Wait for a coroutine to finish, yielding SSE pings every 15 s to prevent idle timeout.
    Yields SSE ping strings while waiting, then yields {"_result": value} once done.
    Raises if the underlying coroutine raised.
    """
    task = asyncio.create_task(coro)
    while True:
        done, _ = await asyncio.wait({task}, timeout=15.0)
        if task in done:
            break
        # task still running — send a keep-alive ping
        yield event({"type": "ping"})
    # This will re-raise any exception from the task
    result = task.result()
    yield {"_result": result}


async def analysis_stream_generator(req: AnalyzeRequest, save_key: str | None = None):
    try:
        clients = get_clients()
        # Use Ollama for simpler/bulk tasks if available, fallback to HF
        client_cheap = clients.get("ollama") or clients.get("hf") or clients.get("anthropic")
        # Use HF for reasoning tasks if available, fallback to Ollama
        client_smart = clients.get("hf") or clients.get("anthropic") or clients.get("ollama")


        # ── Step 1 · Consensus Extraction ─────────────────────────────
        yield event({"step": 1, "status": "loading"})
        s1 = None
        async for chunk in keep_alive(call_llm(
            client_cheap,
            "You extract the consensus default answer most people or AI systems give to a problem, "
            "and score how strongly that consensus converges. "
            "Respond ONLY with valid JSON, no markdown fences or extra text: "
            '{"consensus":"the obvious default answer",'
            '"pressure_score":0.0,'
            '"pressure_label":"weak or moderate or strong or overwhelming"}',
            req.problem,
        )):
            if isinstance(chunk, str):
                yield chunk
            elif isinstance(chunk, dict) and "_result" in chunk:
                s1 = chunk["_result"]

        if s1 is None:
            raise ValueError("Step 1 (consensus) returned no result from LLM.")

        s1 = normalize_llm_output(s1, expected_field="consensus")
        yield event({"step": 1, "status": "done", "data": s1})

        # Determine domain config
        domain_key = (req.domain or "general").strip().lower()
        domain_cfg = DOMAIN_CONFIGS.get(domain_key, DOMAIN_CONFIGS["general"])

        # ── Step 2 · Adversarial Lenses ───────────────────────────────
        yield event({"step": 2, "status": "loading"})
        lens_lines = []
        for k in ["first_principles", "inversion", "analogical", "constraint_removal", "second_order"]:
            lens_lines.append(f"{k}: {domain_cfg.get(k)}")
        lens_descr = "\n".join(lens_lines)
        pattern_hint = ""
        cached_pattern = patterns.get_cached_domain_pattern(domain_key)
        if cached_pattern and cached_pattern.get("total_analyses", 0) > 0 and cached_pattern.get("top_lens"):
            pattern_hint = (
                f"\nHistorical hint: For {domain_key} problems, the {cached_pattern['top_lens']} lens has historically "
                "produced the highest-composite approaches - ensure yours is strong."
            )

        s2 = None
        async for chunk in keep_alive(call_llm(
            client_smart,
            "Generate non-obvious approaches through 5 adversarial lenses, actively working AGAINST the consensus. "
            f"Use these domain framings for domain '{domain_key}':\n{lens_descr}\n"
            f"{pattern_hint}\n"
            "Make each approach specific and actionable. "
            "Respond ONLY with valid JSON, no markdown: "
            '{"approaches":[{"lens":"...","approach":"specific actionable approach","reasoning":"why this diverges from consensus"}]}',
            f"Problem: {req.problem}\n\nConsensus to work against: {s1.get('consensus', '')}\n\n"
            "Generate one genuinely non-obvious approach per lens.",
        )):
            if isinstance(chunk, str):
                yield chunk
            elif isinstance(chunk, dict) and "_result" in chunk:
                s2 = chunk["_result"]

        if s2 is None:
            raise ValueError("Step 2 (lenses) returned no result from LLM.")

        s2 = normalize_llm_output(s2, expected_field="approaches", expected_item_keys=["lens", "approach"])
        raw_approaches = s2.get("approaches", [])
        approaches = []
        for i, item in enumerate(raw_approaches):
            if isinstance(item, dict):
                lens = str(item.get("lens") or item.get("name") or f"lens_{i+1}").strip()
                approach = str(item.get("approach") or item.get("idea") or item.get("text") or "").strip()
                reasoning = str(item.get("reasoning") or "").strip()
                if not approach:
                    approach = json.dumps(item, ensure_ascii=False)
                approaches.append({"lens": lens, "approach": approach, "reasoning": reasoning})
            else:
                approaches.append({"lens": f"lens_{i+1}", "approach": str(item), "reasoning": ""})

        if not approaches:
            approaches = [{
                "lens": "fallback",
                "approach": "Reframe the problem with one contrarian strategy that directly challenges the default consensus.",
                "reasoning": "Used because the model output did not include structured approaches.",
            }]

        s2["approaches"] = approaches
        ap_str = "\n".join(f"{i+1}. [{a.get('lens', f'lens_{i+1}')}] {a.get('approach', '')}" for i, a in enumerate(approaches))
        yield event({"step": 2, "status": "done", "data": s2})

        # ── Step 3 · Validity Scoring ─────────────────────────────────
        yield event({"step": 3, "status": "loading"})
        s3 = None
        async for chunk in keep_alive(call_llm(
            client_cheap,
            "Score each approach on 4 dimensions as 0.0-1.0 floats. "
            "novelty: how much it diverges from consensus. "
            "feasibility: realistic chance of working given real-world constraints. "
            "risk: implementation/failure risk (higher = riskier). "
            "expected_impact: potential upside if it works. "
            "Be honest — not generous. "
            "Respond ONLY with valid JSON, no markdown: "
            '{"scores":[{"lens":"...","novelty":0.0,"feasibility":0.0,"risk":0.0,"expected_impact":0.0}]}',
            f"Problem: {req.problem}\nConsensus baseline: {s1.get('consensus', '')}\nApproaches to score:\n{ap_str}",
        )):
            if isinstance(chunk, str):
                yield chunk
            elif isinstance(chunk, dict) and "_result" in chunk:
                s3 = chunk["_result"]

        if s3 is None:
            raise ValueError("Step 3 (scoring) returned no result from LLM.")

        s3 = normalize_llm_output(s3, expected_field="scores", expected_item_keys=["novelty", "feasibility"])
        yield event({"step": 3, "status": "done", "data": s3})

        # ── Step 4 · Adversarial Critic Pass ──────────────────────────
        yield event({"step": 4, "status": "loading"})
        s4 = None
        async for chunk in keep_alive(call_llm(
            client_smart,
            "You are an adversarial critic. Your job is to break each idea — find specific flawed assumptions, "
            "hidden weaknesses, and realistic failure modes. Be precise and ruthlessly honest. "
            "Respond ONLY with valid JSON, no markdown: "
            '{"critiques":[{"lens":"...","weaknesses":["specific weakness 1","specific weakness 2"],'
            '"failure_modes":["how it fails in practice"],"confidence_drop":0.0}]}',
            f"Problem: {req.problem}\nApproaches to break:\n{ap_str}",
        )):
            if isinstance(chunk, str):
                yield chunk
            elif isinstance(chunk, dict) and "_result" in chunk:
                s4 = chunk["_result"]

        if s4 is None:
            raise ValueError("Step 4 (critic) returned no result from LLM.")

        s4 = normalize_llm_output(s4, expected_field="critiques", expected_item_keys=["weaknesses", "failure_modes"])
        yield event({"step": 4, "status": "done", "data": s4})

        # ── Step 5 · Merge, Rank, Return ──────────────────────────────
        yield event({"step": 5, "status": "loading"})
        merged = []
        for a in approaches:
            sc = next((x for x in s3.get("scores", []) if x.get("lens") == a.get("lens")),
                      {"novelty": 0.5, "feasibility": 0.5, "risk": 0.5, "expected_impact": 0.5})
            cr = next((x for x in s4.get("critiques", []) if x.get("lens") == a.get("lens")),
                      {"weaknesses": [], "failure_modes": [], "confidence_drop": 0.2})
            merged.append({**a, "scores": sc, "critique": cr, "composite": composite_score(sc, cr)})

        merged.sort(key=lambda x: x["composite"], reverse=True)
        result = {"domain": domain_key, "domain_config": domain_cfg, "consensus": s1, "ranked": merged}
        if save_key:
            saved = save_analysis(save_key, req.problem, result)
            result["analysis_id"] = saved["id"]
            result["created_at"] = saved["created_at"]

        yield event({"step": 5, "status": "done", "data": result})
        yield event({"type": "complete", "data": result})

    except Exception as e:
        import traceback
        print(f"[analysis_stream_generator] exception: {e}", flush=True)
        traceback.print_exc()
        yield event({"type": "error", "message": str(e)})


@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    if not req.problem.strip():
        raise HTTPException(status_code=400, detail="problem cannot be empty")

    return StreamingResponse(
        analysis_stream_generator(req),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        },
    )


@app.post("/v1/analyze")
async def v1_analyze(request: Request, req: AnalyzeRequest):
    if not req.problem.strip():
        raise HTTPException(status_code=400, detail="problem cannot be empty")

    # API-key protected versioned endpoint
    key = request.headers.get("x-api-key")
    ok, info = auth.validate_key(key)
    if not ok:
        if info == "missing":
            return JSONResponse({"error": "missing_api_key"}, status_code=401)
        if info == "invalid":
            return JSONResponse({"error": "invalid_api_key"}, status_code=401)
        if info == "rate_limited":
            return JSONResponse({"error": "rate_limited"}, status_code=429)
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # increment usage now (counts this call)
    auth.increment_usage(key)
    usage = auth.usage_info(key)
    remaining = None
    if usage and usage.get("limit_today") is not None:
        remaining = usage["limit_today"] - usage["calls_today"]

    # Prepare headers with rate info
    resp_headers = {"X-Calls-Total": str(usage.get("calls_total", 0) if usage else 0)}
    if remaining is not None:
        resp_headers["X-RateLimit-Remaining"] = str(max(0, remaining))

    # Return the same SSE stream but with rate headers
    return StreamingResponse(
        analysis_stream_generator(req, save_key=key),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            **resp_headers
        },
    )


@app.get("/", response_class=HTMLResponse)
async def root():
    return load_static_html("index.html")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return load_static_html("dashboard.html")


@app.get("/patterns", response_class=HTMLResponse)
async def pattern_library():
    return load_static_html("patterns.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.post("/v1/keys")
async def v1_keys():
    # Generate a new free key; return it once
    key = auth.generate_key(tier="free")
    return JSONResponse({"key": key})


@app.get("/v1/usage")
async def v1_usage(request: Request):
    key = request.headers.get("x-api-key")
    ok, info = auth.validate_key(key)
    if not ok:
        if info == "missing":
            return JSONResponse({"error": "missing_api_key"}, status_code=401)
        return JSONResponse({"error": info}, status_code=401)
    u = auth.usage_info(key)
    return JSONResponse(u or {})


@app.get("/v1/analyses")
async def v1_analyses(request: Request, page: int = 1):
    key = request.headers.get("x-api-key")
    ok, info = auth.validate_key(key)
    if not ok:
        if info == "missing":
            return JSONResponse({"error": "missing_api_key"}, status_code=401)
        return JSONResponse({"error": info}, status_code=401)
    return JSONResponse(list_analyses(key, page=max(1, page), per_page=20))


@app.get("/v1/analyses/{analysis_id}")
async def v1_analysis_detail(analysis_id: str, request: Request):
    key = request.headers.get("x-api-key")
    ok, info = auth.validate_key(key)
    if not ok:
        if info == "missing":
            return JSONResponse({"error": "missing_api_key"}, status_code=401)
        return JSONResponse({"error": info}, status_code=401)
    record = get_analysis_for_key(key, analysis_id)
    if not record:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(record)


@app.get("/v1/patterns")
async def v1_patterns():
    return JSONResponse(patterns.get_all_patterns())


@app.get("/v1/patterns/{domain}")
async def v1_patterns_domain(domain: str):
    return JSONResponse(patterns.compute_patterns(domain))


@app.get("/share/{analysis_id}", response_class=HTMLResponse)
async def share_analysis(analysis_id: str):
    record = get_public_analysis(analysis_id)
    if not record:
        raise HTTPException(status_code=404, detail="analysis not found")

    template = load_static_html("shareable.html")
    hydrated = template.replace(
        "__ANALYSIS_JSON__",
        json.dumps(record, ensure_ascii=False).replace("</", "<\\/"),
    )
    return HTMLResponse(hydrated)
