# Adversarial Framing Engine - Hybrid Pipeline (Hugging Face + Ollama)
import os, json, re, glob, asyncio, secrets, uuid, time, logging, httpx, openai, anthropic
import json_repair
from logging.handlers import RotatingFileHandler
# Load .env file so HF_TOKEN etc. are available without manual env setup
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)
except ImportError:
    pass  # python-dotenv not installed; fall back to system env vars

from typing import Any
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import auth
import patterns
from datetime import datetime
from db import database_enabled, initialize_database
from services import get_job_queue, get_r2_client, get_redis_client, r2_enabled, redis_enabled
from settings import settings

BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
STATIC_DIR = os.path.join(DATA_DIR, "static")
ANALYSES_DIR = os.path.join(DATA_DIR, "analyses")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
REQUEST_LOG_PATH = os.path.join(LOGS_DIR, "requests.log")

DEFAULT_ALLOWED_ORIGINS = ["http://localhost:3000", "http://localhost:8000"]
ACCESS_COOKIE_NAME = "afe_access"
REFRESH_COOKIE_NAME = "afe_refresh"
REAUTH_COOKIE_NAME = "afe_reauth"
COOKIE_SAMESITE = "lax"
COOKIE_SECURE = settings.cookie_secure


def parse_allowed_origins() -> list[str]:
    raw = (os.environ.get("ALLOWED_ORIGINS") or "").strip()
    if not raw:
        return DEFAULT_ALLOWED_ORIGINS
    origins = [item.strip() for item in raw.split(",") if item.strip()]
    for origin in origins:
        if not re.match(r"^https?://", origin):
            raise RuntimeError(f"ALLOWED_ORIGINS contains invalid origin '{origin}'. Include http:// or https://.")
    return origins or DEFAULT_ALLOWED_ORIGINS


ALLOWED_ORIGINS = parse_allowed_origins()


app = FastAPI(
    title="Adversarial Framing Engine API",
    description=(
        "Production API for adversarial framing analysis. Extracts consensus, generates "
        "contrarian lenses, attacks assumptions, scores validity, runs a critic pass, ranks adversarial claims, and "
        "surfaces pattern-library insights across saved analyses."
    ),
    version="1.0.0",
    contact={
        "name": "Adversarial Framing Engine",
        "url": "https://example.com",
        "email": "support@example.com",
    },
    license_info={
        "name": "Proprietary",
        "url": "https://example.com/license",
    },
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Request-ID"],
    expose_headers=["X-Request-ID", "X-Calls-Total", "X-RateLimit-Remaining"],
)

os.makedirs(LOGS_DIR, exist_ok=True)
request_logger = logging.getLogger("afe.requests")
if not request_logger.handlers:
    request_logger.setLevel(logging.INFO)
    request_handler = RotatingFileHandler(
        REQUEST_LOG_PATH,
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    request_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    request_logger.addHandler(request_handler)
    request_logger.propagate = False


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

# Override legacy solution-oriented lens definitions with adversarial attack framings.
DOMAIN_CONFIGS = {
    "general": {
        "first_principles": "identify the premise being treated as natural or inevitable, then attack whether it is actually true under real constraints.",
        "inversion": "assume the stated goal is a proxy, cover story, or misdiagnosis; ask what objective would explain the opposite behavior.",
        "analogical": "map the situation to an unrelated system that failed, decayed, or was captured, and use that analogy to expose hidden structure.",
        "constraint_removal": "remove the socially protected assumption (trust, legitimacy, coordination, patience, compliance) and inspect what immediately breaks.",
        "second_order": "trace what happens if the default answer spreads everywhere; surface the feedback loops, contradictions, and fragilities it creates.",
    },
    "startup": {
        "first_principles": "separate the claimed product value from the actual economic mechanism; test whether the startup is solving the real bottleneck or flattering itself.",
        "inversion": "assume the company is accidentally optimized to look promising rather than become durable; identify what behavior that would predict.",
        "analogical": "compare the startup to speculative bubbles, agency businesses, or distribution arbitrage disguised as product moats; use the analogy to expose weakness.",
        "constraint_removal": "remove investor patience, cheap distribution, and narrative goodwill; inspect whether the model still holds together.",
        "second_order": "if the current tactic works and everyone copies it, show where margins compress, incentives rot, or retention becomes structurally worse.",
    },
    "product": {
        "first_principles": "attack the assumed user need, metric, or workflow; determine whether the product is optimizing a proxy instead of the real job.",
        "inversion": "assume the product is quietly training harmful user behavior or masking a deeper workflow failure; surface that contradiction.",
        "analogical": "map the product to casinos, bureaucracy, addiction loops, or safety systems when useful; use the analogy to reveal the hidden operating logic.",
        "constraint_removal": "remove user trust, patience, and onboarding effort as assumptions; what part of the experience stops making sense immediately?",
        "second_order": "if the product decision scales across the user base, show the downstream distortions, abuse patterns, or ecosystem damage it invites.",
    },
    "engineering": {
        "first_principles": "identify the invariant the system pretends to protect and test whether the architecture actually honors it under stress.",
        "inversion": "assume the current design incentives are selecting for hidden outage modes, silent corruption, or operational theater; expose where.",
        "analogical": "compare the system to domains where failure is catastrophic, or to brittle infrastructures that looked efficient until they cascaded.",
        "constraint_removal": "remove heroic operators, tribal knowledge, and perfect observability as assumptions; what breaks first?",
        "second_order": "if the optimization is applied system-wide, surface the fragility, coupling, or attack surface that compounds over time.",
    },
    "finance": {
        "first_principles": "strip away narrative and ask which cash flows, counterparties, and incentive asymmetries actually determine the outcome.",
        "inversion": "assume the trade is attractive mainly because someone else needs you to hold the risk; inspect that adverse-selection story.",
        "analogical": "map the setup to past crowded trades, insurance mispricing, or reflexive bubbles to reveal where the structure rhymes.",
        "constraint_removal": "remove liquidity, refinancing access, and mark-to-model comfort as assumptions; what becomes obviously fragile?",
        "second_order": "if the strategy is widely copied, trace the reflexive loop, correlation shock, or balance-sheet stress it creates.",
    },
    "research": {
        "first_principles": "identify the hidden assumption in the hypothesis, metric, or experimental design and attack whether it is warranted.",
        "inversion": "assume the result is an artifact, proxy mistake, or selection effect; ask what evidence would make that the better explanation.",
        "analogical": "compare the research pattern to historical replication failures or measurement traps in other fields to expose structural weakness.",
        "constraint_removal": "remove trust in the benchmark, instrumentation, or labeling process as an assumption; what confidence evaporates?",
        "second_order": "if the finding is accepted and operationalized, show the distortions, blind spots, or ethical contradictions that follow.",
    },
}


class ErrorDetail(BaseModel):
    code: str = Field(..., examples=["internal_error"])
    message: str = Field(..., examples=["An unexpected error occurred."])
    request_id: str = Field(..., examples=["4d6f6f7b-2c60-4ac4-b42e-0cd0289ec0e3"])


class ErrorEnvelope(BaseModel):
    error: ErrorDetail


class AnalyzeRequest(BaseModel):
    problem: str = Field(
        ...,
        examples=["Should SaaS companies automate customer support with AI or redesign support from first principles?"],
    )
    domain: str = Field(
        default="general",
        examples=["general"],
    )
    num_results: int = Field(
        default=5,
        ge=1,
        le=10,
        examples=[5],
    )
    verbosity: str = Field(
        default="balanced",
        examples=["compact"],
    )
    pressure_mode: str = Field(
        default="institutional_critique",
        examples=["red_team"],
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "problem": "How should a startup reduce churn without defaulting to discounting?",
                    "domain": "startup",
                    "num_results": 5,
                    "verbosity": "balanced",
                    "pressure_mode": "institutional_critique",
                }
            ]
        }
    }


class SignupRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str
    totp_code: str | None = None


class VerifyEmailRequest(BaseModel):
    token: str


class TotpCodeRequest(BaseModel):
    code: str


class ReauthRequest(BaseModel):
    password: str
    totp_code: str | None = None


class ApiKeyResponse(BaseModel):
    key: str


class AuthUserResponse(BaseModel):
    id: str
    email: str
    email_verified: bool
    mfa_enabled: bool
    security_tier: str
    created_at: str | None = None
    last_login_at: str | None = None


class SessionInfoResponse(BaseModel):
    id: str
    created_at: str | None = None
    last_seen_at: str | None = None
    expires_at: str | None = None
    revoked_at: str | None = None
    ip: str | None = None
    user_agent: str | None = None
    mfa_verified: bool = False
    active: bool = False


class AuthSessionResponse(BaseModel):
    user: AuthUserResponse
    session: SessionInfoResponse
    email_verification_token: str | None = None


class MessageResponse(BaseModel):
    message: str


class VerificationTokenResponse(MessageResponse):
    token: str


class SessionsListResponse(BaseModel):
    current_session_id: str
    sessions: list[SessionInfoResponse]


class MfaSetupResponse(BaseModel):
    secret: str
    otpauth_url: str


class UsageResponse(BaseModel):
    tier: str | None = None
    calls_today: int | None = None
    calls_total: int | None = None
    calls_remaining: int | None = None
    limit_today: int | None = None
    member_since: str | None = None


class AnalysisSummaryResponse(BaseModel):
    id: str | None = None
    problem: str | None = None
    domain: str | None = None
    top_approach: str | None = None
    composite_score: float | None = None
    created_at: str | None = None


class AnalysesPageResponse(BaseModel):
    items: list[AnalysisSummaryResponse]
    page: int
    per_page: int
    total: int
    has_more: bool


class ConsensusResponse(BaseModel):
    consensus: str = ""
    pressure_score: float | None = None
    pressure_label: str | None = None


class LensScoresResponse(BaseModel):
    novelty: float | None = None
    feasibility: float | None = None
    risk: float | None = None
    expected_impact: float | None = None


class CritiqueResponse(BaseModel):
    weaknesses: list[str] = Field(default_factory=list)
    failure_modes: list[str] = Field(default_factory=list)
    confidence_drop: float | None = None


class RankedApproachResponse(BaseModel):
    lens: str | None = None
    approach: str | None = None
    reasoning: str | None = None
    scores: LensScoresResponse | None = None
    critique: CritiqueResponse | None = None
    composite: float | None = None


class AnalysisRecordResponse(BaseModel):
    id: str | None = None
    analysis_id: str | None = None
    problem: str | None = None
    domain: str | None = None
    domain_config: dict[str, Any] = Field(default_factory=dict)
    consensus: ConsensusResponse | dict[str, Any] = Field(default_factory=dict)
    ranked: list[RankedApproachResponse] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None


class PatternResponse(BaseModel):
    domain: str
    top_lens: str | None = None
    lens_win_rates: dict[str, float] = Field(default_factory=dict)
    avg_scores: dict[str, float] = Field(default_factory=dict)
    avg_composite_score: float = 0.0
    top_problem_themes: list[str] = Field(default_factory=list)
    total_analyses: int = 0
    last_updated: str


class AllPatternsResponse(BaseModel):
    domains: dict[str, PatternResponse]
    global_: PatternResponse | None = Field(default=None, alias="global")
    total_analyses: int
    last_updated: str

    model_config = {"populate_by_name": True}


COMMON_ERROR_RESPONSES = {
    400: {"model": ErrorEnvelope, "description": "Bad request."},
    401: {"model": ErrorEnvelope, "description": "Authentication failed or API key is missing."},
    404: {"model": ErrorEnvelope, "description": "Requested resource was not found."},
    429: {"model": ErrorEnvelope, "description": "Rate limit exceeded."},
    500: {"model": ErrorEnvelope, "description": "Internal server error."},
}


def request_id_for(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def mask_api_key(value: str | None) -> str:
    if not value:
        return "-"
    cleaned = str(value).strip()
    return cleaned[:8]


def error_payload(request: Request, code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message, "request_id": request_id_for(request)}}


def error_response(request: Request, status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=error_payload(request, code, message))


def validate_api_key_or_raise(request: Request) -> str:
    key = request.headers.get("x-api-key")
    ok, info = auth.validate_key(key)
    if ok:
        return key
    if info == "missing":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing API key.")
    if info == "invalid":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key.")
    if info == "rate_limited":
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded.")
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized.")


def _client_ip(request: Request) -> str | None:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    if request.client:
        return request.client.host
    return None


def _set_cookie(response: Response, name: str, value: str, max_age: int):
    response.set_cookie(
        key=name,
        value=value,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        max_age=max_age,
        expires=max_age,
        path="/",
    )


def clear_auth_cookies(response: Response):
    response.delete_cookie(ACCESS_COOKIE_NAME, path="/", httponly=True, secure=COOKIE_SECURE, samesite=COOKIE_SAMESITE)
    response.delete_cookie(REFRESH_COOKIE_NAME, path="/", httponly=True, secure=COOKIE_SECURE, samesite=COOKIE_SAMESITE)
    response.delete_cookie(REAUTH_COOKIE_NAME, path="/", httponly=True, secure=COOKIE_SECURE, samesite=COOKIE_SAMESITE)


def attach_auth_cookies(response: Response, user_id: str, session: dict, refresh_token: str):
    access_token = auth.create_access_token(user_id, session["id"], bool(session.get("mfa_verified")))
    _set_cookie(response, ACCESS_COOKIE_NAME, access_token, auth.ACCESS_TOKEN_TTL_SECONDS)
    _set_cookie(response, REFRESH_COOKIE_NAME, refresh_token, auth.REFRESH_TOKEN_TTL_SECONDS)


def attach_reauth_cookie(response: Response, token: str):
    _set_cookie(response, REAUTH_COOKIE_NAME, token, auth.REAUTH_TTL_SECONDS)


def current_user_and_session(request: Request) -> tuple[dict, dict, dict]:
    payload = auth.verify_access_token(request.cookies.get(ACCESS_COOKIE_NAME))
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
    user = auth.get_user(payload.get("sub"))
    session = auth.get_session(payload.get("sid"))
    if not user or not session or not session.get("id") or session.get("user_id") != user.get("id"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session is invalid.")
    if not auth.session_active(session):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session has been revoked.")
    return user, session, payload


def require_verified_user(request: Request) -> tuple[dict, dict, dict]:
    user, session, payload = current_user_and_session(request)
    if not user.get("email_verified"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Email verification required.")
    return user, session, payload


def require_recent_reauth(request: Request, user_id: str, session_id: str):
    token = request.cookies.get(REAUTH_COOKIE_NAME)
    if not token or not auth.verify_reauth_token(token, user_id, session_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Recent re-authentication required.")


def auth_session_payload(user: dict, session: dict, verification_token: str | None = None) -> dict:
    return {
        "user": auth.public_user(user["id"]),
        "session": {
            "id": session.get("id"),
            "created_at": session.get("created_at"),
            "last_seen_at": session.get("last_seen_at"),
            "expires_at": session.get("expires_at"),
            "revoked_at": session.get("revoked_at"),
            "ip": session.get("ip"),
            "user_agent": session.get("user_agent"),
            "mfa_verified": bool(session.get("mfa_verified")),
            "active": auth.session_active(session),
        },
        "email_verification_token": verification_token,
    }


def validate_optional_stripe_key(name: str, value: str | None, prefix: str) -> str:
    if not value:
        return f"{name}: missing"
    if not value.startswith(prefix):
        raise RuntimeError(f"{name} is malformed; expected prefix '{prefix}'.")
    return f"{name}: configured"


def startup_summary_lines() -> list[str]:
    origins_source = "configured" if os.environ.get("ALLOWED_ORIGINS") else "default_fallback"
    lines = [
        f"APP_ENV: {settings.app_env}",
        f"ALLOWED_ORIGINS ({origins_source}): {', '.join(ALLOWED_ORIGINS)}",
        "DATABASE_URL: configured" if settings.database_url else "DATABASE_URL: missing",
        "AUTH_TOKEN_SECRET: configured" if settings.auth_token_secret else "AUTH_TOKEN_SECRET: missing",
        "REDIS: configured" if redis_enabled() else "REDIS: missing",
        "R2: configured" if r2_enabled() else "R2: missing",
        f"JOB_QUEUE_NAME: {settings.job_queue_name}",
        "HF_TOKEN: configured" if os.environ.get("HF_TOKEN") else "HF_TOKEN: missing",
        "OLLAMA_URL: configured" if (os.environ.get("OLLAMA_URL") or os.environ.get("OLLAMA_HOST")) else "OLLAMA_URL: missing",
        "ANTHROPIC_API_KEY: configured" if os.environ.get("ANTHROPIC_API_KEY") else "ANTHROPIC_API_KEY: missing",
        validate_optional_stripe_key("STRIPE_SECRET_KEY", os.environ.get("STRIPE_SECRET_KEY"), "sk_"),
        validate_optional_stripe_key("STRIPE_PUBLISHABLE_KEY", os.environ.get("STRIPE_PUBLISHABLE_KEY"), "pk_"),
        validate_optional_stripe_key("STRIPE_WEBHOOK_SECRET", os.environ.get("STRIPE_WEBHOOK_SECRET"), "whsec_"),
        "Docs: enabled at /docs and /redoc",
    ]
    return lines


@app.on_event("startup")
async def on_startup():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(ANALYSES_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    if database_enabled():
        initialize_database()
    _ = get_redis_client()
    _ = get_job_queue()
    _ = get_r2_client()
    print("[startup] Adversarial Framing Engine API", flush=True)
    for line in startup_summary_lines():
        print(f"[startup] {line}", flush=True)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    if not getattr(request.state, "request_id", None):
        request.state.request_id = str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    started = time.perf_counter()
    if not getattr(request.state, "request_id", None):
        request.state.request_id = str(uuid.uuid4())
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        request_logger.info(
            "method=%s path=%s api_key=%s status=%s duration_ms=%s request_id=%s",
            request.method,
            request.url.path,
            mask_api_key(request.headers.get("x-api-key")),
            status_code,
            elapsed_ms,
            request_id_for(request),
        )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    message = exc.detail if isinstance(exc.detail, str) else "Request failed."
    code_map = {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        429: "rate_limited",
    }
    code = code_map.get(exc.status_code, "http_error")
    return error_response(request, exc.status_code, code, message)


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(request: Request, exc: RequestValidationError):
    message = "; ".join(err.get("msg", "Invalid request.") for err in exc.errors()) or "Invalid request."
    return error_response(request, status.HTTP_400_BAD_REQUEST, "validation_error", message)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    print(f"[unhandled_exception] request_id={request_id_for(request)} error={exc}", flush=True)
    return error_response(
        request,
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "internal_error",
        "An unexpected error occurred.",
    )


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


STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from", "how", "if",
    "in", "into", "is", "it", "its", "of", "on", "or", "that", "the", "their", "them",
    "then", "there", "they", "this", "to", "use", "using", "with", "without", "while",
    "you", "your",
}

SOFT_CONSENSUS_MARKERS = [
    "balance",
    "balanced",
    "human oversight",
    "keep humans",
    "human in the loop",
    "augment",
    "rather than replace",
    "hybrid model",
]

SOLUTIONIST_MARKERS = [
    "build",
    "launch",
    "create",
    "develop",
    "design",
    "implement",
    "roadmap",
    "platform",
    "app",
    "tool",
    "startup",
    "go-to-market",
    "scale",
    "optimize",
    "solution",
]

ATTACK_MARKERS = [
    "assumes",
    "assumption",
    "fragile",
    "fragility",
    "contradiction",
    "tradeoff",
    "tension",
    "fails",
    "failure",
    "breaks",
    "incentive",
    "proxy",
    "externality",
    "bottleneck",
    "collapses",
    "only works",
    "if everyone",
]

ACTION_MARKERS = [
    "mandate",
    "force",
    "ban",
    "audit",
    "publish",
    "split",
    "centralize",
    "decentralize",
    "cap",
    "price",
    "tax",
    "limit",
    "require",
    "shift",
    "replace",
    "stop",
    "start",
    "cut",
    "tie",
    "move",
    "buy",
    "sell",
    "hire",
    "fire",
    "freeze",
    "delay",
    "accelerate",
    "redirect",
    "open",
    "close",
    "rewrite",
    "measure",
]

ABSTRACT_OBSERVATION_MARKERS = [
    "is fragile",
    "creates fragility",
    "reveals",
    "suggests",
    "indicates",
    "often label",
    "tends to",
    "may introduce",
    "could become",
    "functions as",
]


def tokenize_for_overlap(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in STOPWORDS}


def overlap_ratio(a: str, b: str) -> float:
    ta = tokenize_for_overlap(a)
    tb = tokenize_for_overlap(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def looks_soft_consensus(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in SOFT_CONSENSUS_MARKERS)


def looks_solutionist(text: str) -> bool:
    low = (text or "").lower()
    return sum(1 for marker in SOLUTIONIST_MARKERS if marker in low) >= 2


def has_attack_language(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in ATTACK_MARKERS)


def looks_actionable(text: str) -> bool:
    low = (text or "").lower()
    if any(marker in low for marker in ACTION_MARKERS):
        return True
    return bool(re.search(r"\b(do|make|turn|use|run|create|build|implement|enforce|assign)\b", low))


def looks_purely_observational(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in ABSTRACT_OBSERVATION_MARKERS) and not looks_actionable(low)


def normalize_domain(value: str | None) -> str:
    cleaned = (value or "").strip().lower()
    if cleaned in DOMAIN_CONFIGS:
        return cleaned
    if cleaned:
        print(f"[warn] unknown domain '{cleaned}' requested; falling back to 'general'", flush=True)
    return "general"


def normalize_verbosity(value: str | None) -> str:
    cleaned = (value or "").strip().lower()
    if cleaned in {"compact", "balanced", "detailed"}:
        return cleaned
    return "balanced"


PRESSURE_MODES = {
    "red_team": "Attack the dominant story as if your job is to find the failure path before an opponent does. Prioritize hidden weaknesses, exploitability, and brittle confidence.",
    "institutional_critique": "Treat institutions, incentives, and legitimacy as the main explanatory variables. Prioritize power, dependency, governance, and narrative cover.",
    "economic_pressure": "Interpret the problem through incentives, rent extraction, cost shifting, concentration, and mispriced externalities.",
    "systems_collapse": "Prioritize bottlenecks, cascade risk, hidden coupling, delayed failure, and nonlinear breakdown.",
    "strategic_risk": "Focus on adversarial exposure, asymmetric downside, coordination traps, and decision-making under uncertainty.",
    "psychological_pressure": "Focus on status motives, self-deception, signaling, fear management, and emotional narratives disguising themselves as logic.",
    "geopolitical": "Treat power blocs, supply dependencies, sovereignty, bargaining leverage, and strategic capture as first-order variables.",
    "founder_stress_test": "Interrogate whether ambition, narrative, and growth logic are masking fragility, false moats, or unsound economics.",
    "narrative_warfare": "Focus on frame control, legitimacy manufacturing, selective disclosure, and whose interests the dominant story protects.",
    "coordination_failure": "Prioritize misaligned incentives, collective action costs, local rationality creating global failure, and trust dependence.",
}


def normalize_pressure_mode(value: str | None) -> str:
    cleaned = (value or "").strip().lower().replace(" ", "_")
    if cleaned in PRESSURE_MODES:
        return cleaned
    return "institutional_critique"


def verbosity_instruction(value: str) -> str:
    if value == "compact":
        return "Keep each approach and reasoning compressed: 1 sentence each, high signal, no filler."
    if value == "detailed":
        return "Allow slightly more detail: up to 3 sentences for approach and 2 for reasoning, but stay sharp and concrete."
    return "Keep each approach concise: 1-2 sentences for approach and 1 sentence for reasoning."


def heuristic_question_type(problem: str) -> str:
    low = (problem or "").strip().lower()
    if any(marker in low for marker in ["challenge", "attack", "debunk", "critique", "is this wrong", "argue against"]):
        return "adversarial"
    if any(marker in low for marker in ["what should we do", "how do we", "how should we operate", "next week", "execute", "operational"]):
        return "operational"
    if any(marker in low for marker in ["best path", "strategy", "strategic", "where should we go", "what should our strategy"]):
        return "strategic"
    if any(marker in low for marker in ["what is true", "what assumptions", "analyze", "why is", "what explains", "what's really happening"]):
        return "analytical"
    return "strategic"


def question_type_badge(value: str) -> str:
    return f"{value} question"


def build_default_scores(approaches: list[dict]) -> list[dict]:
    defaults = []
    for i, item in enumerate(approaches):
        defaults.append({
            "lens": item.get("lens") or f"lens_{i+1}",
            "novelty": 0.5,
            "feasibility": 0.5,
            "risk": 0.5,
            "expected_impact": 0.5,
        })
    return defaults


def is_consensus_adjacent(consensus: str, approach: str) -> bool:
    low = (approach or "").lower()
    if any(marker in low for marker in SOFT_CONSENSUS_MARKERS):
        return True
    if overlap_ratio(consensus, approach) >= 0.38:
        return True
    return False


def assess_divergence(consensus: str, approaches: list[dict], require_actionable: bool = False) -> dict:
    issues = []
    adjacent = 0
    duplicate_pairs = 0
    solutionist = 0
    low_attack_signal = 0
    non_actionable = 0
    observational_only = 0

    for item in approaches:
        approach_text = item.get("approach", "")
        reasoning_text = item.get("reasoning", "")
        combined = f"{approach_text} {reasoning_text}".strip()
        if is_consensus_adjacent(consensus, approach_text):
            adjacent += 1
        if looks_solutionist(combined):
            solutionist += 1
        if not has_attack_language(combined):
            low_attack_signal += 1
        if require_actionable:
            if not looks_actionable(approach_text):
                non_actionable += 1
            if looks_purely_observational(approach_text):
                observational_only += 1

    for i in range(len(approaches)):
        for j in range(i + 1, len(approaches)):
            if overlap_ratio(approaches[i].get("approach", ""), approaches[j].get("approach", "")) >= 0.45:
                duplicate_pairs += 1

    if looks_soft_consensus(consensus):
        issues.append("consensus_is_soft")
    if adjacent >= 2:
        issues.append("approaches_too_close_to_consensus")
    if duplicate_pairs >= 2:
        issues.append("approaches_not_distinct")
    if solutionist >= 2:
        issues.append("too_solution_oriented")
    if low_attack_signal >= 3:
        issues.append("not_enough_assumption_or_fragility_language")
    if require_actionable and non_actionable >= 2:
        issues.append("approaches_not_actionable_enough")
    if require_actionable and observational_only >= 2:
        issues.append("approaches_too_observational")
    if len(approaches) < 5:
        issues.append("missing_lenses")

    return {
        "ok": len(issues) == 0,
        "issues": issues,
        "adjacent": adjacent,
        "duplicate_pairs": duplicate_pairs,
        "solutionist": solutionist,
        "low_attack_signal": low_attack_signal,
        "non_actionable": non_actionable,
        "observational_only": observational_only,
    }


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
    return ((n * 0.30 + f * 0.28 + ei * 0.34) * (1 - r * 0.18)) * (1 - cd * 0.42)


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
        "settings": result.get("settings", {}),
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
    """Wait for a coroutine to finish, yielding SSE pings every 5 s to prevent idle timeout.
    Yields SSE ping strings while waiting, then yields {"_result": value} once done.
    Raises if the underlying coroutine raised.
    """
    task = asyncio.create_task(coro)
    while True:
        done, _ = await asyncio.wait({task}, timeout=5.0)
        if task in done:
            break
        # task still running — send a keep-alive ping
        yield event({"type": "ping"})
    # This will re-raise any exception from the task
    result = task.result()
    yield {"_result": result}


async def analysis_stream_generator(req: AnalyzeRequest, save_key: str | None = None, request_id: str | None = None):
    try:
        clients = get_clients()
        domain_key = normalize_domain(req.domain)
        verbosity = normalize_verbosity(req.verbosity)
        pressure_mode = normalize_pressure_mode(req.pressure_mode)
        pressure_instruction = PRESSURE_MODES[pressure_mode]
        num_results = max(1, min(int(req.num_results or 5), 10))
        question_type = heuristic_question_type(req.problem)
        # Use Ollama for simpler/bulk tasks if available, fallback to HF
        client_cheap = clients.get("ollama") or clients.get("hf") or clients.get("anthropic")
        # Use HF for reasoning tasks if available, fallback to Ollama
        client_smart = clients.get("hf") or clients.get("anthropic") or clients.get("ollama")


        # ── Step 1 · Consensus Extraction ─────────────────────────────
        yield event({"step": 1, "status": "loading"})
        s1 = None
        async for chunk in keep_alive(call_llm(
            client_cheap,
            f"You are SARE, a strategic adversarial reasoning engine. Extract the hidden consensus frame most people, executives, or generic AI systems would assume in the domain '{domain_key}'. "
            "CRITICAL OUTPUT QUALITY RULES:\n"
            "1. EVERY OUTPUT MUST BE SEMANTICALLY COHERENT. Reject random noun chains, disconnected abstractions, or meaningless intensity wording. Every sentence must have clear meaning.\n"
            "2. NO TOKEN COLLAPSE. Never continue generation using momentum-based associative text drift. Do not stack abstract nouns endlessly.\n"
            "3. EACH CONSENSUS MUST CONTAIN EXACTLY ONE CORE IDEA. Avoid mixing unrelated concepts.\n"
            "4. FORCE CONCRETE LANGUAGE. Prefer institutional, economic, operational, and strategic language. Avoid mystical phrasing, abstract overload, or fake profundity.\n"
            "5. MAXIMUM CLARITY OVER MAXIMUM COMPLEXITY. The goal is sharpness and strategic insight, not sounding philosophical or academic.\n"
            "6. PRIORITIZE INSIGHT DENSITY. The consensus should feel compact, memorable, and human-written. Avoid filler and excessive explanation.\n"
            "7. HARD FAILURE CONDITION: If a generated sentence cannot be paraphrased into a clear strategic observation, it is invalid and must be rewritten.\n"
            "Do not merely restate the user's sentence. Compress the dominant framing, implied causal model, and hidden dependency into a sharper underlying consensus claim. "
            "Do not soften it with caveats, balancing language, or moral hedging. "
            "State the dominant prescription or premise in its bluntest plausible form, including the key hidden assumption if it is obvious. "
            "This is not the best answer; it is the socially legible answer that others will tend to defend. "
            "Pressure score measures how strongly answers converge on that same default framing. "
            "Respond ONLY with valid JSON, no markdown fences or extra text: "
            '{"consensus":"the obvious default answer",'
            '"pressure_score":0.0,'
            '"pressure_label":"weak or moderate or strong or overwhelming"}',
            f"Problem: {req.problem}\n"
            f"Selected domain: {domain_key}\n"
            f"Pressure mode: {pressure_mode}\n"
            "Return the strongest default framing, not the balanced implementation advice that usually follows it.",
        )):
            if isinstance(chunk, str):
                yield chunk
            elif isinstance(chunk, dict) and "_result" in chunk:
                s1 = chunk["_result"]

        if s1 is None:
            raise ValueError("Step 1 (consensus) returned no result from LLM.")

        s1 = normalize_llm_output(s1, expected_field="consensus")
        s1["domain"] = domain_key
        s1["verbosity"] = verbosity
        s1["num_results"] = num_results
        s1["pressure_mode"] = pressure_mode
        s1["question_type"] = question_type
        s1["question_type_badge"] = question_type_badge(question_type)
        yield event({"step": 1, "status": "done", "data": s1})

        # Step 1.5 · Question Type Detection
        qt_result = None
        async for chunk in keep_alive(call_llm(
            client_cheap,
            "Classify the user's problem into one of exactly four buckets for an adversarial framing pipeline. "
            "Buckets: operational, analytical, strategic, adversarial. "
            "Respond ONLY with valid JSON: "
            '{"question_type":"operational or analytical or strategic or adversarial"}',
            f"Problem: {req.problem}\nSelected domain: {domain_key}\nPressure mode: {pressure_mode}\nReturn only the best-fit label.",
        )):
            if isinstance(chunk, dict) and "_result" in chunk:
                qt_result = chunk["_result"]

        try:
            if qt_result is not None:
                qt_result = normalize_llm_output(qt_result, expected_field="question_type")
                candidate = str(qt_result.get("question_type") or "").strip().lower()
                if candidate in {"operational", "analytical", "strategic", "adversarial"}:
                    question_type = candidate
        except Exception as exc:
            print(f"[step1.5] question type parse failed; using heuristic fallback '{question_type}': {exc}", flush=True)

        s1["question_type"] = question_type
        s1["question_type_badge"] = question_type_badge(question_type)

        yield event({
            "type": "context",
            "data": {
                "domain": domain_key,
                "pressure_mode": pressure_mode,
                "question_type": question_type,
                "question_type_badge": question_type_badge(question_type),
            },
        })

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

        interrogation_mode = question_type in {"analytical", "adversarial"}
        if interrogation_mode:
            type_instruction = (
                "This is an analytical/adversarial question. Each lens must produce an ASSUMPTION INTERROGATION. "
                "Expose what the narrative takes for granted, who benefits from that framing, and what it deliberately obscures. "
                "Output an insight plus its implication, not an action plan."
            )
        else:
            type_instruction = (
                "This is an operational/strategic question, but the output must still stay observational. "
                "Diagnose where action would fail, what incentives distort the field, and what hidden dependency makes the system fragile. "
                "Do not convert the answer into advice or implementation guidance."
            )
        output_schema = '{"approaches":[{"lens":"...","approach":"specific strategic observation","reasoning":"what it exposes, who benefits, and what second-order instability it reveals"}]}'

        s2 = None
        lens_system = (
            "You are SARE: Strategic Adversarial Reasoning Engine. Generate 5 adversarial lenses that stress-test the consensus through diagnosis rather than advice. "
            "CRITICAL OUTPUT QUALITY RULES:\n"
            "1. EVERY OUTPUT MUST BE SEMANTICALLY COHERENT. Reject random noun chains, disconnected abstractions, or meaningless intensity wording. Every sentence must have clear meaning.\n"
            "2. NO TOKEN COLLAPSE. Never continue generation using momentum-based associative text drift. Do not stack abstract nouns endlessly.\n"
            "3. EACH LENS MUST CONTAIN EXACTLY ONE CORE IDEA. Focus on one contradiction, fragility, incentive, or tension per lens. Avoid mixing unrelated concepts.\n"
            "4. FORCE CONCRETE LANGUAGE. Prefer institutional, economic, operational, and strategic language. Avoid mystical phrasing, abstract overload, or fake profundity.\n"
            "5. MAXIMUM CLARITY OVER MAXIMUM COMPLEXITY. The goal is sharpness and strategic insight, not sounding philosophical or academic.\n"
            "6. PRIORITIZE INSIGHT DENSITY. Each lens should feel compact, memorable, and human-written. Avoid filler and excessive explanation.\n"
            "7. HARD FAILURE CONDITION: If a generated sentence cannot be paraphrased into a clear strategic observation, it is invalid and must be rewritten.\n"
            "Approaches that could appear in a balanced essay, a product brief, or a consultancy memo are rejected. "
            f"Use these domain framings for domain '{domain_key}':\n{lens_descr}\n"
            f"{pattern_hint}\n"
            f"{verbosity_instruction(verbosity)} "
            f"Pressure mode '{pressure_mode}': {pressure_instruction} "
            f"{type_instruction} "
            "Each lens must expose one of the following: hidden incentives, structural contradictions, fragile dependencies, coordination failures, market conditioning, narrative asymmetries, or long-term strategic traps. "
            "Do not generate product advice, business recommendations, optimization suggestions, implementation guidance, startup ideas, policy blueprints, motivational advice, or generic recommendations. "
            "Reject anything that sounds like polished consultant advice, innovation brainstorming, safe executive synthesis, or how-to guidance. "
            "The output should feel strategically uncomfortable, compressed, insight-dense, and hard to forget. "
            "The 5 lenses must come from genuinely different reasoning structures: incentives, fragility, coordination, power, narrative, dependency, governance, psychology, economic structure, or second-order systems dynamics. "
            "At least one lens may partially defend the consensus conditionally, at least one should attack it directly, and at least one should reframe the premise entirely. Avoid artificial agreement. "
            + (
                "For each lens: approach is the observation. reasoning states its implication, who benefits from the framing, what the framing obscures, and what second-order instability follows if the consensus spreads. "
                if interrogation_mode else
                "For each lens: approach is a strategic observation, not a recommendation or action plan. "
                "Avoid verbs like create, implement, develop, establish, design, propose, launch, or optimize unless they are unavoidable. "
                "Before finalizing each approach, check: (a) does it expose a hidden incentive, structural contradiction, fragile dependency, coordination failure, or narrative asymmetry? "
                "(b) does it resist turning into advice? "
                "(c) would an expert feel this changed the way the problem is framed? If any answer is no, rewrite. "
                "For each lens: approach is the compressed strategic observation. reasoning is why that observation bites and what assumption, incentive, contradiction, fragility, or second-order effect it exposes. "
            )
            + "Respond ONLY with valid JSON, no markdown: "
            + output_schema
        )
        lens_user = (
            f"Problem: {req.problem}\n"
            f"Selected domain: {domain_key}\n"
            f"Pressure mode: {pressure_mode}\n"
            f"Requested results: {num_results}\n"
            f"Verbosity: {verbosity}\n\n"
            f"Question type: {question_type}\n\n"
            f"Consensus to work against: {s1.get('consensus', '')}\n\n"
            "Generate exactly one genuinely adversarial output per lens: first_principles, inversion, analogical, "
            "constraint_removal, second_order.\n"
            "Default to adversarial lenses, assumption attacks, fragility analysis, and systemic contradictions.\n"
            + (
                "Each output should be an insight plus implication, not an action."
                if interrogation_mode else
                "Each output should be a strategic interpretation or pressure move, not a solution framework."
            )
        )
        async for chunk in keep_alive(call_llm(client_smart, lens_system, lens_user)):
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
                "approach": (
                    "The dominant narrative is likely protecting an unstated assumption about incentives, control, or risk allocation."
                    if interrogation_mode else
                    "Mandate a formal review that isolates the consensus assumption most likely to fail, assigns an owner to attack it, and changes policy only after that red-team pass."
                ),
                "reasoning": (
                    "Used because the model output did not include structured assumption interrogations."
                    if interrogation_mode else
                    "Used because the model output did not include structured adversarial strategies."
                ),
            }]

        divergence_check = assess_divergence(s1.get("consensus", ""), approaches, require_actionable=False)
        if not divergence_check["ok"]:
            retry_feedback = ", ".join(divergence_check["issues"]) or "insufficient divergence"
            retry_user = (
                f"{lens_user}\n\n"
                f"Your previous attempt failed this check: {retry_feedback}.\n"
                "Do not repeat hybrid, balanced, oversight-heavy, or solution-oriented versions of the consensus.\n"
                "Force distance: at least 3 outputs should feel surprising or initially uncomfortable to a mainstream operator.\n"
                "At least 4 outputs must explicitly attack an assumption, contradiction, incentive, or fragility.\n"
                + (
                    "Every output must expose what the framing takes for granted, who benefits from it, and what it obscures.\n"
                    if interrogation_mode else
                    "Every output must expose a structural tension, contradiction, strategic instability, or second-order consequence rather than devolving into a solution pitch.\n"
                )
                + f"Rejected prior approaches:\n{json.dumps(approaches, ensure_ascii=False)}"
            )
            s2_retry = None
            async for chunk in keep_alive(call_llm(client_smart, lens_system, retry_user)):
                if isinstance(chunk, str):
                    yield chunk
                elif isinstance(chunk, dict) and "_result" in chunk:
                    s2_retry = chunk["_result"]

            if s2_retry is not None:
                s2_retry = normalize_llm_output(s2_retry, expected_field="approaches", expected_item_keys=["lens", "approach"])
                retry_approaches = []
                for i, item in enumerate(s2_retry.get("approaches", [])):
                    if isinstance(item, dict):
                        lens = str(item.get("lens") or item.get("name") or f"lens_{i+1}").strip()
                        approach = str(item.get("approach") or item.get("idea") or item.get("text") or "").strip()
                        reasoning = str(item.get("reasoning") or "").strip()
                        if not approach:
                            approach = json.dumps(item, ensure_ascii=False)
                        retry_approaches.append({"lens": lens, "approach": approach, "reasoning": reasoning})
                    else:
                        retry_approaches.append({"lens": f"lens_{i+1}", "approach": str(item), "reasoning": ""})

                retry_check = assess_divergence(s1.get("consensus", ""), retry_approaches, require_actionable=not interrogation_mode)
                if retry_approaches and (
                    retry_check["ok"]
                    or retry_check["adjacent"] < divergence_check["adjacent"]
                    or retry_check["duplicate_pairs"] < divergence_check["duplicate_pairs"]
                ):
                    approaches = retry_approaches
                    s2 = s2_retry

        s2["approaches"] = approaches
        s2["domain"] = domain_key
        s2["pressure_mode"] = pressure_mode
        s2["question_type"] = question_type
        s2["question_type_badge"] = question_type_badge(question_type)
        s2["verbosity"] = verbosity
        s2["num_results"] = num_results
        ap_str = "\n".join(f"{i+1}. [{a.get('lens', f'lens_{i+1}')}] {a.get('approach', '')}" for i, a in enumerate(approaches))
        yield event({"step": 2, "status": "done", "data": s2})

        # ── Step 3 · Validity Scoring ─────────────────────────────────
        yield event({"step": 3, "status": "loading"})
        s3 = None
        async for chunk in keep_alive(call_llm(
            client_cheap,
            "Score each approach on 4 dimensions as 0.0-1.0 floats. "
            "novelty: how non-obvious, insight-dense, and cognitively disruptive the lens is. "
            "feasibility: how defensible the reasoning is under real-world incentives, constraints, and institutional behavior. "
            "risk: how much systemic fragility, strategic instability, or contradiction the lens exposes if true (higher = more dangerous). "
            "expected_impact: how much explanatory power, reframing force, and decision-changing pressure the lens carries. "
            "Reward contradiction exposure, hidden incentive awareness, second-order depth, and anti-groupthink differentiation. Be honest, skeptical, and not generous. "
            "Respond ONLY with valid JSON, no markdown: "
            '{"scores":[{"lens":"...","novelty":0.0,"feasibility":0.0,"risk":0.0,"expected_impact":0.0}]}',
            f"Problem: {req.problem}\nSelected domain: {domain_key}\nPressure mode: {pressure_mode}\nVerbosity: {verbosity}\nConsensus baseline: {s1.get('consensus', '')}\nApproaches to score:\n{ap_str}",
        )):
            if isinstance(chunk, str):
                yield chunk
            elif isinstance(chunk, dict) and "_result" in chunk:
                s3 = chunk["_result"]

        if s3 is None:
            print(f"[step3] no score response; using defaults for domain={domain_key} question_type={question_type}", flush=True)
            s3 = {"scores": build_default_scores(approaches)}
        else:
            try:
                s3 = normalize_llm_output(s3, expected_field="scores", expected_item_keys=["novelty", "feasibility"])
                if not isinstance(s3.get("scores"), list) or not s3.get("scores"):
                    raise ValueError("scores field missing or not a list")
            except Exception as exc:
                print(f"[step3] malformed score response; raw={json.dumps(s3, ensure_ascii=False)[:1500]} error={exc}", flush=True)
                s3 = {"scores": build_default_scores(approaches)}

        s3["domain"] = domain_key
        s3["pressure_mode"] = pressure_mode
        s3["question_type"] = question_type
        s3["question_type_badge"] = question_type_badge(question_type)
        s3["verbosity"] = verbosity
        s3["num_results"] = num_results
        yield event({"step": 3, "status": "done", "data": s3})

        # ── Step 4 · Adversarial Critic Pass ──────────────────────────
        yield event({"step": 4, "status": "loading"})
        s4 = None
        async for chunk in keep_alive(call_llm(
            client_smart,
            "You are SARE's hostile reviewer. Break each lens by finding where even the adversarial critique overreaches. "
            "CRITICAL OUTPUT QUALITY RULES:\n"
            "1. EVERY OUTPUT MUST BE SEMANTICALLY COHERENT. Reject random noun chains, disconnected abstractions, or meaningless intensity wording. Every sentence must have clear meaning.\n"
            "2. NO TOKEN COLLAPSE. Never continue generation using momentum-based associative text drift. Do not stack abstract nouns endlessly.\n"
            "3. EACH CRITIQUE MUST CONTAIN EXACTLY ONE CORE IDEA per point. Avoid mixing unrelated concepts.\n"
            "4. FORCE CONCRETE LANGUAGE. Prefer institutional, economic, operational, and strategic language. Avoid mystical phrasing, abstract overload, or fake profundity.\n"
            "5. MAXIMUM CLARITY OVER MAXIMUM COMPLEXITY. The goal is sharpness and strategic insight, not sounding philosophical or academic.\n"
            "6. PRIORITIZE INSIGHT DENSITY. Each critique should feel compact, memorable, and human-written. Avoid filler and excessive explanation.\n"
            "7. HARD FAILURE CONDITION: If a generated sentence cannot be paraphrased into a clear strategic observation, it is invalid and must be rewritten.\n"
            "Attack weak logic, ideological bias, unsupported claims, emotional persuasion disguised as reasoning, shallow contrarianism, and false dichotomies. "
            "Find specific flawed assumptions, blind spots, hidden weaknesses, and realistic failure modes. Be precise and ruthlessly honest, not summarizing. "
            "Respond ONLY with valid JSON, no markdown: "
            '{"critiques":[{"lens":"...","weaknesses":["specific weakness 1","specific weakness 2"],'
            '"failure_modes":["how it fails in practice"],"confidence_drop":0.0}]}',
            f"Problem: {req.problem}\nSelected domain: {domain_key}\nPressure mode: {pressure_mode}\nQuestion type: {question_type}\nApproaches to break:\n{ap_str}",
        )):
            if isinstance(chunk, str):
                yield chunk
            elif isinstance(chunk, dict) and "_result" in chunk:
                s4 = chunk["_result"]

        if s4 is None:
            raise ValueError("Step 4 (critic) returned no result from LLM.")

        s4 = normalize_llm_output(s4, expected_field="critiques", expected_item_keys=["weaknesses", "failure_modes"])
        s4["domain"] = domain_key
        s4["pressure_mode"] = pressure_mode
        s4["question_type"] = question_type
        s4["question_type_badge"] = question_type_badge(question_type)
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
        merged = merged[:num_results]
        result = {
            "domain": domain_key,
            "pressure_mode": pressure_mode,
            "question_type": question_type,
            "question_type_badge": question_type_badge(question_type),
            "domain_config": domain_cfg,
            "consensus": s1,
            "ranked": merged,
            "settings": {"num_results": num_results, "verbosity": verbosity, "pressure_mode": pressure_mode},
        }
        if save_key:
            saved = save_analysis(save_key, req.problem, result)
            result["analysis_id"] = saved["id"]
            result["created_at"] = saved["created_at"]

        yield event({"step": 5, "status": "done", "data": result})
        yield event({"type": "complete", "data": result})

    except Exception as e:
        import traceback
        print(f"[analysis_stream_generator] request_id={request_id or 'unknown'} exception: {e}", flush=True)
        traceback.print_exc()
        yield event({
            "type": "error",
            "message": "An unexpected error occurred.",
            "error": {
                "code": "internal_error",
                "message": "An unexpected error occurred.",
                "request_id": request_id or "unknown",
            },
        })


@app.post(
    "/analyze",
    response_model=None,
    summary="Run anonymous adversarial analysis",
    description="Streams the five-step adversarial framing pipeline without API-key authentication.",
    tags=["analysis"],
    responses=COMMON_ERROR_RESPONSES,
)
async def analyze(request: Request, req: AnalyzeRequest):
    if not req.problem.strip():
        raise HTTPException(status_code=400, detail="problem cannot be empty")

    return StreamingResponse(
        analysis_stream_generator(req, request_id=request_id_for(request)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        },
    )


@app.post(
    "/v1/analyze",
    response_model=None,
    summary="Run authenticated adversarial analysis",
    description="Streams a saved adversarial analysis, increments usage, and attaches rate-limit headers.",
    tags=["analysis"],
    responses=COMMON_ERROR_RESPONSES,
)
async def v1_analyze(request: Request, req: AnalyzeRequest):
    if not req.problem.strip():
        raise HTTPException(status_code=400, detail="problem cannot be empty")

    key = validate_api_key_or_raise(request)

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
        analysis_stream_generator(req, save_key=key, request_id=request_id_for(request)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            **resp_headers
        },
    )


@app.get(
    "/",
    response_model=None,
    response_class=HTMLResponse,
    summary="Serve engine UI",
    description="Returns the main adversarial framing engine HTML interface.",
    tags=["ui"],
    responses=COMMON_ERROR_RESPONSES,
)
async def root():
    return load_static_html("index.html")


@app.get(
    "/dashboard",
    response_model=None,
    response_class=HTMLResponse,
    summary="Serve dashboard UI",
    description="Returns the authenticated dashboard for usage and saved analyses.",
    tags=["ui"],
    responses=COMMON_ERROR_RESPONSES,
)
async def dashboard():
    return load_static_html("dashboard.html")


@app.get(
    "/auth",
    response_model=None,
    response_class=HTMLResponse,
    summary="Serve auth UI",
    description="Returns the authentication console for signup, login, verification, MFA, and secure key generation.",
    tags=["ui"],
    responses=COMMON_ERROR_RESPONSES,
)
async def auth_console():
    return load_static_html("auth.html")


@app.get(
    "/patterns",
    response_model=None,
    response_class=HTMLResponse,
    summary="Serve pattern library UI",
    description="Returns the public pattern library page backed by aggregate analysis data.",
    tags=["ui"],
    responses=COMMON_ERROR_RESPONSES,
)
async def pattern_library():
    return load_static_html("patterns.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.post(
    "/auth/signup",
    response_model=AuthSessionResponse,
    summary="Create account",
    description="Registers a user with email and password, creates a signed session, and returns an email verification token.",
    tags=["auth"],
    responses=COMMON_ERROR_RESPONSES,
)
async def auth_signup(request: Request, body: SignupRequest, response: Response):
    ok, result = auth.create_user(body.email, body.password)
    if not ok:
        reasons = {
            "invalid_email": "A valid email address is required.",
            "weak_password": "Password must be at least 10 characters.",
            "email_taken": "An account with that email already exists.",
        }
        raise HTTPException(status_code=400, detail=reasons.get(result.get("reason"), "Unable to create account."))
    user = auth.get_user(result["user"]["id"])
    verification_token = auth.create_email_verification_token(user["id"])
    refresh_token, session = auth.create_session(
        user["id"],
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        mfa_verified=False,
    )
    attach_auth_cookies(response, user["id"], session, refresh_token)
    return auth_session_payload(user, session, verification_token)


@app.post(
    "/auth/login",
    response_model=AuthSessionResponse,
    summary="Log in",
    description="Authenticates a user with email and password, optionally requiring TOTP when MFA is enabled.",
    tags=["auth"],
    responses=COMMON_ERROR_RESPONSES,
)
async def auth_login(request: Request, body: LoginRequest, response: Response):
    ok, result = auth.authenticate_user(body.email, body.password)
    if not ok:
        if result.get("reason") == "locked":
            raise HTTPException(status_code=429, detail=f"Account locked until {result.get('locked_until')}.")
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    user = result["user"]
    mfa = user.get("mfa") or {}
    if mfa.get("enabled"):
        if not auth.verify_totp(mfa.get("secret", ""), body.totp_code or ""):
            raise HTTPException(status_code=401, detail="Valid TOTP code required.")
    auth.complete_login_success(user["id"])
    user = auth.get_user(user["id"])
    verification_token = None if user.get("email_verified") else auth.create_email_verification_token(user["id"])
    refresh_token, session = auth.create_session(
        user["id"],
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        mfa_verified=bool(mfa.get("enabled")),
    )
    attach_auth_cookies(response, user["id"], session, refresh_token)
    return auth_session_payload(user, session, verification_token)


@app.post(
    "/auth/refresh",
    response_model=AuthSessionResponse,
    summary="Refresh session",
    description="Rotates the refresh token and issues a new short-lived access token.",
    tags=["auth"],
    responses=COMMON_ERROR_RESPONSES,
)
async def auth_refresh(request: Request, response: Response):
    refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Refresh token missing.")
    ok, result = auth.rotate_refresh_session(refresh_token, _client_ip(request), request.headers.get("user-agent"))
    if not ok:
        raise HTTPException(status_code=401, detail="Refresh token is invalid or expired.")
    session = result["session"]
    user = auth.get_user(session["user_id"])
    if not user:
        clear_auth_cookies(response)
        raise HTTPException(status_code=401, detail="Session user no longer exists.")
    attach_auth_cookies(response, user["id"], session, result["refresh_token"])
    return auth_session_payload(user, session)


@app.post(
    "/auth/logout",
    response_model=MessageResponse,
    summary="Log out",
    description="Revokes the current refresh session and clears auth cookies.",
    tags=["auth"],
    responses=COMMON_ERROR_RESPONSES,
)
async def auth_logout(request: Request, response: Response):
    payload = auth.verify_access_token(request.cookies.get(ACCESS_COOKIE_NAME))
    if payload:
        auth.revoke_session(payload.get("sid"))
    clear_auth_cookies(response)
    return {"message": "Logged out."}


@app.get(
    "/auth/me",
    response_model=AuthSessionResponse,
    summary="Get current session",
    description="Returns the current authenticated user and session metadata from the access cookie.",
    tags=["auth"],
    responses=COMMON_ERROR_RESPONSES,
)
async def auth_me(request: Request):
    user, session, _ = current_user_and_session(request)
    return auth_session_payload(user, session)


@app.post(
    "/auth/email-verification/request",
    response_model=VerificationTokenResponse,
    summary="Request email verification",
    description="Creates a fresh verification token for the current authenticated account.",
    tags=["auth"],
    responses=COMMON_ERROR_RESPONSES,
)
async def auth_request_email_verification(request: Request):
    user, _, _ = current_user_and_session(request)
    token = auth.create_email_verification_token(user["id"])
    return {"message": "Verification token issued.", "token": token}


@app.post(
    "/auth/email-verification/verify",
    response_model=AuthUserResponse,
    summary="Verify email",
    description="Marks the current account email as verified using a time-limited verification token.",
    tags=["auth"],
    responses=COMMON_ERROR_RESPONSES,
)
async def auth_verify_email(body: VerifyEmailRequest):
    ok, result = auth.verify_email_token(body.token)
    if not ok:
        raise HTTPException(status_code=400, detail="Verification token is invalid, expired, or already used.")
    return result["user"]


@app.post(
    "/auth/mfa/setup",
    response_model=MfaSetupResponse,
    summary="Start TOTP setup",
    description="Creates a pending TOTP secret for the authenticated account.",
    tags=["auth"],
    responses=COMMON_ERROR_RESPONSES,
)
async def auth_mfa_setup(request: Request):
    user, _, _ = require_verified_user(request)
    ok, result = auth.start_mfa_setup(user["id"])
    if not ok:
        raise HTTPException(status_code=400, detail="Unable to start MFA setup.")
    return result


@app.post(
    "/auth/mfa/confirm",
    response_model=AuthUserResponse,
    summary="Confirm TOTP setup",
    description="Verifies a TOTP code and enables MFA for the authenticated account.",
    tags=["auth"],
    responses=COMMON_ERROR_RESPONSES,
)
async def auth_mfa_confirm(request: Request, body: TotpCodeRequest):
    user, _, _ = require_verified_user(request)
    ok, result = auth.confirm_mfa_setup(user["id"], body.code)
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid TOTP code.")
    return result["user"]


@app.post(
    "/auth/reauth",
    response_model=MessageResponse,
    summary="Re-authenticate sensitive action",
    description="Revalidates password and TOTP, then issues a short-lived re-auth cookie for sensitive endpoints.",
    tags=["auth"],
    responses=COMMON_ERROR_RESPONSES,
)
async def auth_reauth(request: Request, body: ReauthRequest, response: Response):
    user, session, _ = current_user_and_session(request)
    if not auth.authenticate_user(user["email"], body.password)[0]:
        raise HTTPException(status_code=401, detail="Password challenge failed.")
    mfa = user.get("mfa") or {}
    if mfa.get("enabled") and not auth.verify_totp(mfa.get("secret", ""), body.totp_code or ""):
        raise HTTPException(status_code=401, detail="Valid TOTP code required.")
    token = auth.create_reauth_token(user["id"], session["id"])
    attach_reauth_cookie(response, token)
    return {"message": "Re-authentication complete."}


@app.get(
    "/auth/sessions",
    response_model=SessionsListResponse,
    summary="List sessions",
    description="Returns all sessions associated with the current account.",
    tags=["auth"],
    responses=COMMON_ERROR_RESPONSES,
)
async def auth_sessions(request: Request):
    user, session, _ = current_user_and_session(request)
    return {"current_session_id": session["id"], "sessions": auth.list_sessions(user["id"])}


@app.post(
    "/auth/sessions/logout-all",
    response_model=MessageResponse,
    summary="Log out all other sessions",
    description="Revokes every refresh session for the current user except the active one.",
    tags=["auth"],
    responses=COMMON_ERROR_RESPONSES,
)
async def auth_logout_all_sessions(request: Request):
    user, session, _ = current_user_and_session(request)
    auth.revoke_all_sessions(user["id"], except_session_id=session["id"])
    return {"message": "All other sessions revoked."}


@app.post(
    "/v1/keys",
    response_model=ApiKeyResponse,
    summary="Create API key",
    description="Generates a new free-tier API key for the authenticated, verified user after recent re-authentication.",
    tags=["auth"],
    responses=COMMON_ERROR_RESPONSES,
)
async def v1_keys(request: Request):
    user, session, _ = require_verified_user(request)
    require_recent_reauth(request, user["id"], session["id"])
    key = auth.generate_key(name=user["email"], tier="free", owner_user_id=user["id"])
    return {"key": key}


@app.get(
    "/v1/usage",
    response_model=UsageResponse,
    summary="Get usage",
    description="Returns the current authenticated key's usage and rate-limit information.",
    tags=["auth"],
    responses=COMMON_ERROR_RESPONSES,
)
async def v1_usage(request: Request):
    key = validate_api_key_or_raise(request)
    u = auth.usage_info(key)
    return u or {}


@app.get(
    "/v1/analyses",
    response_model=AnalysesPageResponse,
    summary="List saved analyses",
    description="Returns paginated saved analyses for the authenticated key.",
    tags=["analysis"],
    responses=COMMON_ERROR_RESPONSES,
)
async def v1_analyses(request: Request, page: int = 1):
    key = validate_api_key_or_raise(request)
    return list_analyses(key, page=max(1, page), per_page=20)


@app.get(
    "/v1/analyses/{analysis_id}",
    response_model=AnalysisRecordResponse,
    summary="Get saved analysis",
    description="Returns a full saved analysis record for the authenticated key.",
    tags=["analysis"],
    responses=COMMON_ERROR_RESPONSES,
)
async def v1_analysis_detail(analysis_id: str, request: Request):
    key = validate_api_key_or_raise(request)
    record = get_analysis_for_key(key, analysis_id)
    if not record:
        raise HTTPException(status_code=404, detail="Analysis not found.")
    return record


@app.get(
    "/v1/patterns",
    response_model=AllPatternsResponse,
    summary="Get all pattern aggregates",
    description="Returns public pattern data across all supported domains plus global aggregates.",
    tags=["patterns"],
    responses=COMMON_ERROR_RESPONSES,
)
async def v1_patterns():
    return patterns.get_all_patterns()


@app.get(
    "/v1/patterns/{domain}",
    response_model=PatternResponse,
    summary="Get domain pattern aggregates",
    description="Returns public pattern data for a single domain.",
    tags=["patterns"],
    responses=COMMON_ERROR_RESPONSES,
)
async def v1_patterns_domain(domain: str):
    return patterns.compute_patterns(domain)


@app.get(
    "/share/{analysis_id}",
    response_model=None,
    response_class=HTMLResponse,
    summary="Serve shareable analysis page",
    description="Returns a public HTML page for a saved analysis by ID.",
    tags=["ui"],
    responses=COMMON_ERROR_RESPONSES,
)
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


@app.get(
    "/shareable",
    response_model=None,
    response_class=HTMLResponse,
    summary="Serve shareable page preview",
    description="Returns a sample public share page for UI preview and QA.",
    tags=["ui"],
    responses=COMMON_ERROR_RESPONSES,
)
async def shareable_preview():
    sample = {
        "problem": "How should a small team reduce release risk while shipping faster?",
        "domain": "engineering",
        "created_at": utc_now_iso(),
        "consensus": {
            "consensus": "Add more approvals and longer QA cycles before every release.",
            "pressure_label": "strong",
        },
        "ranked": [
            {
                "lens": "first_principles",
                "approach": "Shift to progressive delivery with per-feature guardrails and rapid rollback.",
                "composite": 0.781,
                "scores": {
                    "novelty": 0.76,
                    "feasibility": 0.82,
                    "risk": 0.35,
                    "expected_impact": 0.88,
                },
                "critique": {
                    "weaknesses": ["Requires investment in observability", "Needs release discipline"],
                    "failure_modes": ["Rollback not tested regularly"],
                },
            }
        ],
    }
    template = load_static_html("shareable.html")
    hydrated = template.replace(
        "__ANALYSIS_JSON__",
        json.dumps(sample, ensure_ascii=False).replace("</", "<\\/"),
    )
    return HTMLResponse(hydrated)
