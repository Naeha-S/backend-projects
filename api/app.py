import os, json, re
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import anthropic

app = FastAPI(title="Adversarial Framing Engine")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def get_client():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError("ANTHROPIC_API_KEY environment variable is not set. Run: export ANTHROPIC_API_KEY=your_key_here")
    return anthropic.AsyncAnthropic(api_key=key)


class AnalyzeRequest(BaseModel):
    problem: str


async def call_llm(client, system: str, user: str) -> dict:
    msg = await client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in msg.content if hasattr(b, "text"))
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError(f"No JSON found in model response: {text[:300]}")
    return json.loads(m.group(0))


def composite_score(s: dict, c: dict) -> float:
    n  = s.get("novelty", 0.5)
    f  = s.get("feasibility", 0.5)
    r  = s.get("risk", 0.5)
    ei = s.get("expected_impact", 0.5)
    cd = c.get("confidence_drop", 0.2)
    return ((n * 0.25 + f * 0.35 + ei * 0.35) * (1 - r * 0.15)) * (1 - cd * 0.3)


def event(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    if not req.problem.strip():
        raise HTTPException(status_code=400, detail="problem cannot be empty")

    async def stream():
        try:
            client = get_client()

            # ── Step 1 · Consensus Extraction ─────────────────────────────
            yield event({"step": 1, "status": "loading"})
            s1 = await call_llm(
                client,
                "You extract the consensus default answer most people or AI systems give to a problem, "
                "and score how strongly that consensus converges. "
                "Respond ONLY with valid JSON, no markdown fences or extra text: "
                '{"consensus":"the obvious default answer",'
                '"pressure_score":0.0,'
                '"pressure_label":"weak or moderate or strong or overwhelming"}',
                req.problem,
            )
            yield event({"step": 1, "status": "done", "data": s1})

            # ── Step 2 · Adversarial Lenses ───────────────────────────────
            yield event({"step": 2, "status": "loading"})
            s2 = await call_llm(
                client,
                "Generate non-obvious approaches through 5 adversarial lenses, actively working AGAINST the consensus. "
                "Lenses: "
                "first_principles (strip all assumptions, rebuild from zero), "
                "inversion (solve the opposite problem and see what it reveals), "
                "analogical (find a solved version in a completely unrelated domain, apply that solution), "
                "constraint_removal (the biggest assumed constraint doesn't exist — what's now possible?), "
                "second_order (the obvious solution creates a new problem — solve that instead). "
                "Make each approach specific and actionable. "
                "Respond ONLY with valid JSON, no markdown: "
                '{"approaches":[{"lens":"...","approach":"specific actionable approach","reasoning":"why this diverges from consensus"}]}',
                f"Problem: {req.problem}\n\nConsensus to work against: {s1['consensus']}\n\n"
                "Generate one genuinely non-obvious approach per lens.",
            )
            approaches = s2.get("approaches", [])
            ap_str = "\n".join(f"{i+1}. [{a['lens']}] {a['approach']}" for i, a in enumerate(approaches))
            yield event({"step": 2, "status": "done", "data": s2})

            # ── Step 3 · Validity Scoring ─────────────────────────────────
            yield event({"step": 3, "status": "loading"})
            s3 = await call_llm(
                client,
                "Score each approach on 4 dimensions as 0.0-1.0 floats. "
                "novelty: how much it diverges from consensus. "
                "feasibility: realistic chance of working given real-world constraints. "
                "risk: implementation/failure risk (higher = riskier). "
                "expected_impact: potential upside if it works. "
                "Be honest — not generous. "
                "Respond ONLY with valid JSON, no markdown: "
                '{"scores":[{"lens":"...","novelty":0.0,"feasibility":0.0,"risk":0.0,"expected_impact":0.0}]}',
                f"Problem: {req.problem}\nConsensus baseline: {s1['consensus']}\nApproaches to score:\n{ap_str}",
            )
            yield event({"step": 3, "status": "done", "data": s3})

            # ── Step 4 · Adversarial Critic Pass ──────────────────────────
            yield event({"step": 4, "status": "loading"})
            s4 = await call_llm(
                client,
                "You are an adversarial critic. Your job is to break each idea — find specific flawed assumptions, "
                "hidden weaknesses, and realistic failure modes. Be precise and ruthlessly honest. "
                "Respond ONLY with valid JSON, no markdown: "
                '{"critiques":[{"lens":"...","weaknesses":["specific weakness 1","specific weakness 2"],'
                '"failure_modes":["how it fails in practice"],"confidence_drop":0.0}]}',
                f"Problem: {req.problem}\nApproaches to break:\n{ap_str}",
            )
            yield event({"step": 4, "status": "done", "data": s4})

            # ── Step 5 · Merge, Rank, Return ──────────────────────────────
            yield event({"step": 5, "status": "loading"})
            merged = []
            for a in approaches:
                sc = next((x for x in s3.get("scores", []) if x["lens"] == a["lens"]),
                          {"novelty": 0.5, "feasibility": 0.5, "risk": 0.5, "expected_impact": 0.5})
                cr = next((x for x in s4.get("critiques", []) if x["lens"] == a["lens"]),
                          {"weaknesses": [], "failure_modes": [], "confidence_drop": 0.2})
                merged.append({**a, "scores": sc, "critique": cr, "composite": composite_score(sc, cr)})

            merged.sort(key=lambda x: x["composite"], reverse=True)
            result = {"consensus": s1, "ranked": merged}

            yield event({"step": 5, "status": "done", "data": result})
            yield event({"type": "complete", "data": result})

        except Exception as e:
            yield event({"type": "error", "message": str(e)})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/", response_class=HTMLResponse)
async def root():
    html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(html_path, encoding="utf-8") as f:
        return f.read()


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
