import glob
import json
import os
import re
from collections import Counter
from datetime import datetime

import auth


BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
ANALYSES_DIR = os.path.join(DATA_DIR, "analyses")
PATTERN_RECORDS_PATH = os.path.join(DATA_DIR, "pattern_records.jsonl")
PATTERNS_CACHE_PATH = os.path.join(DATA_DIR, "patterns_cache.json")

LENSES = [
    "first_principles",
    "inversion",
    "analogical",
    "constraint_removal",
    "second_order",
]

DOMAINS = [
    "general",
    "startup",
    "product",
    "engineering",
    "finance",
    "research",
]

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how", "i",
    "if", "in", "into", "is", "it", "of", "on", "or", "our", "so", "that", "the",
    "their", "them", "they", "this", "to", "we", "what", "when", "where", "why",
    "with", "you", "your", "about", "after", "all", "already", "around", "been",
    "before", "between", "both", "can", "could", "did", "do", "does", "each", "few",
    "get", "got", "had", "has", "have", "having", "here", "more", "most", "much",
    "need", "needs", "now", "off", "only", "other", "out", "over", "same", "should",
    "some", "than", "then", "there", "these", "those", "too", "under", "very", "via",
    "want", "will", "would",
}

ALIASES = {
    "customers": "customer",
    "users": "user",
    "retaining": "retention",
    "retention": "retention",
    "pricing": "pricing",
    "prices": "pricing",
    "priced": "pricing",
    "growth": "growth",
    "acquisition": "acquisition",
    "churn": "retention",
    "engagement": "engagement",
    "conversion": "conversion",
    "revenue": "revenue",
    "monetization": "monetization",
    "latency": "latency",
    "reliability": "reliability",
    "performance": "performance",
    "onboarding": "onboarding",
    "distribution": "distribution",
    "team": "team",
    "hiring": "hiring",
}


def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _read_json(path: str):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: str, payload: dict):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


def _iter_pattern_records():
    if not os.path.exists(PATTERN_RECORDS_PATH):
        return
    with open(PATTERN_RECORDS_PATH, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _iter_saved_analysis_records():
    for path in glob.glob(os.path.join(ANALYSES_DIR, "*", "*.json")):
        try:
            saved = _read_json(path)
        except Exception:
            continue
        yield _build_record(
            key="",
            problem=saved.get("problem", ""),
            domain=saved.get("domain", "general"),
            ranked_results=saved.get("ranked") or [],
        )


def _tokenize_problem(problem: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", (problem or "").lower())
    themes = []
    for word in words:
        theme = ALIASES.get(word, word)
        if theme in STOPWORDS or theme.isdigit():
            continue
        themes.append(theme)
    return themes


def _build_record(key: str, problem: str, domain: str, ranked_results: list[dict]) -> dict:
    ranked = ranked_results or []
    top = ranked[0] if ranked else {}
    lens_scores = {}
    composites = {}
    for item in ranked:
        lens = item.get("lens")
        if not lens:
            continue
        scores = item.get("scores") or {}
        lens_scores[lens] = {
            "novelty": float(scores.get("novelty", 0.0) or 0.0),
            "feasibility": float(scores.get("feasibility", 0.0) or 0.0),
            "risk": float(scores.get("risk", 0.0) or 0.0),
            "expected_impact": float(scores.get("expected_impact", 0.0) or 0.0),
        }
        composites[lens] = float(item.get("composite", 0.0) or 0.0)

    return {
        "key_hash": auth.key_hash(key) if key else None,
        "problem": problem,
        "domain": (domain or "general").strip().lower() or "general",
        "created_at": utc_now_iso(),
        "winning_lens": top.get("lens"),
        "winning_composite": float(top.get("composite", 0.0) or 0.0),
        "lens_scores": lens_scores,
        "composites": composites,
        "themes": _tokenize_problem(problem),
    }


def record_analysis(key, problem, domain, ranked_results):
    _ensure_data_dir()
    record = _build_record(key, problem, domain, ranked_results)
    with open(PATTERN_RECORDS_PATH, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def _blank_pattern(domain: str, total_analyses: int = 0, last_updated: str | None = None) -> dict:
    return {
        "domain": domain,
        "top_lens": None,
        "lens_win_rates": {lens: 0.0 for lens in LENSES},
        "avg_scores": {
            "novelty": 0.0,
            "feasibility": 0.0,
            "risk": 0.0,
            "expected_impact": 0.0,
        },
        "avg_composite_score": 0.0,
        "top_problem_themes": [],
        "total_analyses": total_analyses,
        "last_updated": last_updated or utc_now_iso(),
    }


def _count_saved_analyses() -> int:
    return len(glob.glob(os.path.join(ANALYSES_DIR, "*", "*.json")))


def _load_source_records() -> list[dict]:
    saved_total = _count_saved_analyses()
    recorded = list(_iter_pattern_records() or [])
    if recorded and len(recorded) >= saved_total:
        return recorded
    saved_records = list(_iter_saved_analysis_records())
    if saved_records:
        return saved_records
    return recorded


def _compute_pattern_from_records(records: list[dict], domain: str) -> dict:
    if not records:
        return _blank_pattern(domain)

    win_counter = Counter()
    theme_counter = Counter()
    score_totals = {
        "novelty": 0.0,
        "feasibility": 0.0,
        "risk": 0.0,
        "expected_impact": 0.0,
    }
    score_count = 0
    composite_total = 0.0

    for record in records:
        winning_lens = record.get("winning_lens")
        if winning_lens in LENSES:
            win_counter[winning_lens] += 1
        composite_total += float(record.get("winning_composite", 0.0) or 0.0)
        theme_counter.update(record.get("themes") or [])
        for lens_scores in (record.get("lens_scores") or {}).values():
            for field in score_totals:
                score_totals[field] += float(lens_scores.get(field, 0.0) or 0.0)
            score_count += 1

    total = len(records)
    avg_scores = {
        field: round((score_totals[field] / score_count), 4) if score_count else 0.0
        for field in score_totals
    }
    lens_win_rates = {
        lens: round((win_counter.get(lens, 0) / total), 4) if total else 0.0
        for lens in LENSES
    }
    top_lens = max(LENSES, key=lambda lens: lens_win_rates[lens]) if total else None

    return {
        "domain": domain,
        "top_lens": top_lens if total else None,
        "lens_win_rates": lens_win_rates,
        "avg_scores": avg_scores,
        "avg_composite_score": round((composite_total / total), 4) if total else 0.0,
        "top_problem_themes": [item for item, _ in theme_counter.most_common(5)],
        "total_analyses": total,
        "last_updated": utc_now_iso(),
    }


def _read_cache():
    if not os.path.exists(PATTERNS_CACHE_PATH):
        return None
    try:
        return _read_json(PATTERNS_CACHE_PATH)
    except Exception:
        return None


def _cache_is_fresh(cache: dict | None, current_total: int) -> bool:
    if not cache:
        return False
    cached_total = int(cache.get("analysis_count", 0) or 0)
    if current_total < cached_total:
        return False
    return current_total < cached_total + 10


def _rebuild_cache():
    records = _load_source_records()
    global_pattern = _compute_pattern_from_records(records, "global")
    by_domain = {}
    for domain in DOMAINS:
        domain_records = [record for record in records if record.get("domain") == domain]
        by_domain[domain] = _compute_pattern_from_records(domain_records, domain)

    payload = {
        "analysis_count": _count_saved_analyses(),
        "last_updated": utc_now_iso(),
        "domains": by_domain,
        "global": global_pattern,
    }
    _write_json(PATTERNS_CACHE_PATH, payload)
    return payload


def _get_cache():
    current_total = _count_saved_analyses()
    cache = _read_cache()
    if _cache_is_fresh(cache, current_total):
        return cache
    return _rebuild_cache()


def compute_patterns(domain):
    domain_key = (domain or "general").strip().lower() or "general"
    cache = _get_cache()
    pattern = (cache.get("domains") or {}).get(domain_key)
    if pattern:
        return pattern
    return _blank_pattern(domain_key, last_updated=cache.get("last_updated"))


def get_global_patterns():
    cache = _get_cache()
    return cache.get("global") or _blank_pattern("global", last_updated=cache.get("last_updated"))


def get_all_patterns():
    cache = _get_cache()
    return {
        "domains": cache.get("domains") or {domain: _blank_pattern(domain) for domain in DOMAINS},
        "global": cache.get("global") or _blank_pattern("global"),
        "total_analyses": int(cache.get("analysis_count", 0) or 0),
        "last_updated": cache.get("last_updated") or utc_now_iso(),
    }


def get_cached_domain_pattern(domain):
    domain_key = (domain or "general").strip().lower() or "general"
    cache = _read_cache()
    if not cache:
        return None
    return (cache.get("domains") or {}).get(domain_key)
