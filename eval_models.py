#!/usr/bin/env python3
"""
Model bake-off harness for the dilution-extraction pipeline.

Compares LLM backends head-to-head on the REAL pipeline — DeepSeek V4 Flash vs
Gemini 2.5 Flash by default (Groq GPT-OSS-120B available, disabled by default).

How it stays apples-to-apples
------------------------------
It monkeypatches ONLY `smart_dilution_extractor.llm_extractor.extract_with_llm`,
so every backend sees the exact same system prompt, few-shot, JSON schema, and
the same rules/XBRL/validators/reconcile layers around it. The ONLY variable is
which model answers each chunk. That isolates model quality from pipeline logic.

What it measures
----------------
* SPEED   — per-call latency, output tokens, tokens/sec, total LLM wall time.
* COST    — from real token usage x each model's price (cache-aware where the
            API reports cached tokens).
* ACCURACY — graded BY YOU. The harness can't know ground truth, so it writes a
            per-ticker review doc listing every model's extracted events in a
            readable table. You eyeball each one, fill scoring_template.csv with
            true-positives / false-positives / missed, and re-run in `score`
            mode to get precision / recall / F1 per model.

Usage
-----
    # 1. set keys (env vars, or edit the placeholders below)
    export DEEPSEEK_API_KEY=sk-...
    export GEMINI_API_KEY=...
    # export GROQ_API_KEY=...            # only if you enable the groq provider

    # 2. edit TICKERS (put your ~20), then run the extraction pass:
    backend/venv/bin/python eval_models.py run

    # 3. open eval_results/<TICKER>__review.md, grade each model's events,
    #    fill the TP/FP/missed columns in eval_results/scoring_template.csv

    # 4. compute the leaderboard:
    backend/venv/bin/python eval_models.py score

Run with the BACKEND venv (it has httpx + pydantic + the extractor deps):
    /path/to/sec-dilution-tracker/backend/venv/bin/python
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from pathlib import Path

import httpx

# --- make the extractor importable when run from the repo root --------------
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from smart_dilution_extractor import llm_extractor as le  # noqa: E402
from smart_dilution_extractor import run as run_pipeline  # noqa: E402
from smart_dilution_extractor.models import LLMExtraction  # noqa: E402

# ===========================================================================
# CONFIG — edit these
# ===========================================================================

# API keys. Prefer env vars; the strings here are PLACEHOLDERS — replace or set
# the env var. A provider with an unset/placeholder key is skipped automatically.
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "REPLACE_WITH_DEEPSEEK_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "REPLACE_WITH_GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "REPLACE_WITH_GROQ_API_KEY")

# The 20 tickers to test. Put yours here (a few real ones pre-filled as examples).
TICKERS = [
    "IONQ", "POET", "SIDU", "OUST",
    # --- add ~16 more below ---
    "TICKER05", "TICKER06", "TICKER07", "TICKER08", "TICKER09", "TICKER10",
    "TICKER11", "TICKER12", "TICKER13", "TICKER14", "TICKER15", "TICKER16",
    "TICKER17", "TICKER18", "TICKER19", "TICKER20",
]

# How many filings per stock (matches your backend default of 12).
MAX_FILINGS = 12

# Output directory for dumps / review docs / scoring sheet.
OUT_DIR = _REPO_ROOT / "eval_results"

# Per-call HTTP timeout (seconds) and retry attempts on 429/5xx.
HTTP_TIMEOUT = 120.0
MAX_RETRIES = 2

# Each provider: endpoint, model id, pricing ($/1M tokens), and an `enabled`
# flag. Prices are May-2026 list prices — adjust if they move. Set enabled=True
# on groq to add GPT-OSS-120B to the bake-off.
PROVIDERS = {
    "deepseek_v4_flash": {
        "enabled": True,
        "kind": "openai",  # OpenAI-compatible REST
        "url": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-v4-flash",
        "api_key": DEEPSEEK_API_KEY,
        "price_in": 0.14, "price_out": 0.28, "price_cached_in": 0.0028,
    },
    "gemini_2.5_flash": {
        "enabled": True,
        "kind": "gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        "model": "gemini-2.5-flash",
        "api_key": GEMINI_API_KEY,
        "price_in": 0.30, "price_out": 2.50, "price_cached_in": 0.03,
    },
    "groq_gpt_oss_120b": {
        "enabled": False,  # flip to True to include it
        "kind": "openai",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "model": "openai/gpt-oss-120b",
        "api_key": GROQ_API_KEY,
        "price_in": 0.15, "price_out": 0.60, "price_cached_in": 0.15,  # no cache on Groq
    },
}

# ===========================================================================
# Prompt assembly — identical for every provider (fairness).
# We reuse the extractor's own SYSTEM_PROMPT + FEW_SHOT, and append the JSON
# schema to the system message because (unlike Ollama's `format=`) these REST
# APIs aren't doing constrained decoding here — we rely on prompt + validation.
# ===========================================================================
_SCHEMA_JSON = json.dumps(LLMExtraction.model_json_schema())
_SYSTEM = (
    le.SYSTEM_PROMPT
    + "\n\nYour JSON output MUST conform to this JSON schema:\n"
    + _SCHEMA_JSON
)


def _build_messages(chunk: str) -> tuple[str, str]:
    """Return (system_text, user_text) matching the production prompt shape."""
    user = f'{le.FEW_SHOT}\n\nNOW EXTRACT FROM THIS TEXT:\n"""\n{chunk}\n"""'
    return _SYSTEM, user


# ===========================================================================
# Provider HTTP calls. Each returns: (raw_text, in_tok, out_tok, cached_tok).
# ===========================================================================
def _post_with_retry(client: httpx.Client, **kwargs) -> httpx.Response:
    last = None
    for attempt in range(MAX_RETRIES + 1):
        resp = client.post(**kwargs)
        if resp.status_code < 400:
            return resp
        last = resp
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
            time.sleep(1.5 * (attempt + 1))
            continue
        break
    last.raise_for_status()
    return last  # unreachable


def _call_openai_compatible(cfg: dict, chunk: str, client: httpx.Client):
    system, user = _build_messages(chunk)
    resp = _post_with_retry(
        client,
        url=cfg["url"],
        headers={"Authorization": f"Bearer {cfg['api_key']}",
                 "Content-Type": "application/json"},
        json={
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        },
    )
    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {}) or {}
    in_tok = usage.get("prompt_tokens", 0)
    out_tok = usage.get("completion_tokens", 0)
    # DeepSeek reports cache hits here; absent elsewhere.
    cached = (usage.get("prompt_cache_hit_tokens")
              or usage.get("prompt_tokens_details", {}).get("cached_tokens", 0) or 0)
    return text, in_tok, out_tok, cached


def _call_gemini(cfg: dict, chunk: str, client: httpx.Client):
    system, user = _build_messages(chunk)
    url = cfg["url"].format(model=cfg["model"])
    resp = _post_with_retry(
        client,
        url=url,
        params={"key": cfg["api_key"]},
        headers={"Content-Type": "application/json"},
        json={
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": 0.0,
                "responseMimeType": "application/json",
                # Disable "thinking" so we measure fast extraction, not reasoning
                # latency. (Gemini 2.5 Flash also supports a strict responseSchema
                # — left off here to keep the json-mode approach uniform across
                # providers; enable it if you want to test Gemini's strict mode.)
                "thinkingConfig": {"thinkingBudget": 0},
            },
        },
    )
    data = resp.json()
    cand = data["candidates"][0]
    text = "".join(p.get("text", "") for p in cand["content"]["parts"])
    um = data.get("usageMetadata", {}) or {}
    in_tok = um.get("promptTokenCount", 0)
    out_tok = um.get("candidatesTokenCount", 0)
    cached = um.get("cachedContentTokenCount", 0) or 0
    return text, in_tok, out_tok, cached


_CALLERS = {"openai": _call_openai_compatible, "gemini": _call_gemini}


# ===========================================================================
# The patched extract_with_llm: calls a provider, validates, records stats.
# ===========================================================================
def make_extractor(cfg: dict, stats: dict, client: httpx.Client):
    caller = _CALLERS[cfg["kind"]]

    def extract_with_llm(chunk: str) -> LLMExtraction:
        t0 = time.perf_counter()
        try:
            text, in_tok, out_tok, cached = caller(cfg, chunk, client)
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            stats["latencies"].append(time.perf_counter() - t0)
            return LLMExtraction(events=[], overall_confidence="low",
                                 notes=f"http_error: {exc!r}")
        dt = time.perf_counter() - t0

        # Parse + validate; one lenient retry handled by the caller's own retry,
        # here we just try to coerce the JSON into the schema.
        result = LLMExtraction(events=[], overall_confidence="low", notes="parse_fail")
        try:
            result = LLMExtraction.model_validate(json.loads(text))
            for ev in result.events:
                ev.extraction_method = "llm"
        except Exception as exc:  # noqa: BLE001
            stats["parse_errors"] += 1
            result.notes = f"parse_error: {exc!r}"

        stats["calls"] += 1
        stats["latencies"].append(dt)
        stats["in_tok"] += in_tok
        stats["out_tok"] += out_tok
        stats["cached_tok"] += cached
        return result

    return extract_with_llm


# ===========================================================================
# Run pass
# ===========================================================================
def _event_row(ev) -> dict:
    return {
        "type": getattr(ev.offering_type, "value", str(ev.offering_type)),
        "category": getattr(ev, "dilution_category", ""),
        "status": ev.status,
        "shares": ev.shares,
        "price": ev.price_per_share,
        "gross_usd": ev.gross_proceeds_usd,
        "date": ev.event_date or ev.filing_date or "",
        "form": ev.source_form,
        "confidence": ev.confidence,
        "quote": (ev.source_quote or "")[:160],
    }


def _est_cost(cfg: dict, stats: dict) -> float:
    fresh_in = max(stats["in_tok"] - stats["cached_tok"], 0)
    return (
        fresh_in / 1e6 * cfg["price_in"]
        + stats["cached_tok"] / 1e6 * cfg["price_cached_in"]
        + stats["out_tok"] / 1e6 * cfg["price_out"]
    )


def run_pass():
    active = {k: v for k, v in PROVIDERS.items() if v["enabled"]}
    for name, cfg in list(active.items()):
        if not cfg["api_key"] or cfg["api_key"].startswith("REPLACE_WITH_"):
            print(f"⚠️  {name}: API key not set — skipping. "
                  f"(set {name.split('_')[0].upper()}_API_KEY)")
            active.pop(name)
    if not active:
        print("No providers with valid keys. Set keys and retry.")
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_rows = []
    # ticker -> provider -> list[event_row]   (for the review docs)
    review: dict[str, dict[str, list]] = {}

    orig_avail = le._ollama_available
    orig_extract = le.extract_with_llm
    le._ollama_available = lambda: True  # force the LLM path on

    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            for ticker in TICKERS:
                if ticker.startswith("TICKER"):
                    continue  # skip placeholder slots
                review.setdefault(ticker, {})
                for name, cfg in active.items():
                    stats = {"calls": 0, "errors": 0, "parse_errors": 0,
                             "latencies": [], "in_tok": 0, "out_tok": 0,
                             "cached_tok": 0}
                    le.extract_with_llm = make_extractor(cfg, stats, client)

                    print(f"▶ {ticker:8} | {name} ...", flush=True)
                    wall0 = time.perf_counter()
                    try:
                        res = run_pipeline(ticker, max_filings=MAX_FILINGS,
                                           use_llm=True, verbose=False)
                        events = res.events
                    except Exception as exc:  # noqa: BLE001
                        print(f"   !! pipeline failed: {exc!r}")
                        events = []
                    wall = time.perf_counter() - wall0

                    rows = [_event_row(e) for e in events]
                    review[ticker][name] = rows
                    (OUT_DIR / f"{ticker}__{name}.events.json").write_text(
                        json.dumps(rows, indent=2, default=str))

                    lat = stats["latencies"]
                    total_llm = sum(lat)
                    tps = (stats["out_tok"] / total_llm) if total_llm else 0.0
                    summary_rows.append({
                        "ticker": ticker,
                        "provider": name,
                        "n_events": len(rows),
                        "n_active": sum(1 for r in rows if r["category"] == "active"),
                        "n_potential": sum(1 for r in rows if r["category"] != "active"),
                        "llm_calls": stats["calls"],
                        "errors": stats["errors"],
                        "parse_errors": stats["parse_errors"],
                        "total_llm_s": round(total_llm, 2),
                        "avg_call_s": round(total_llm / len(lat), 3) if lat else 0,
                        "out_tokens": stats["out_tok"],
                        "tokens_per_s": round(tps, 1),
                        "est_cost_usd": round(_est_cost(cfg, stats), 5),
                        "pipeline_wall_s": round(wall, 2),
                    })
    finally:
        le._ollama_available = orig_avail
        le.extract_with_llm = orig_extract

    _write_summary(summary_rows)
    _write_reviews(review)
    _write_scoring_template(summary_rows)
    print(f"\n✅ Done. See {OUT_DIR}/")
    print("   1. summary.csv         — speed + cost leaderboard")
    print("   2. <TICKER>__review.md — grade each model's events by hand")
    print("   3. scoring_template.csv — fill TP/FP/missed, then: eval_models.py score")


def _write_summary(rows):
    if not rows:
        return
    path = OUT_DIR / "summary.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    # also an aggregate-by-provider view
    agg: dict[str, dict] = {}
    for r in rows:
        a = agg.setdefault(r["provider"], {
            "tickers": 0, "events": 0, "llm_calls": 0, "total_llm_s": 0.0,
            "out_tokens": 0, "est_cost_usd": 0.0, "errors": 0, "parse_errors": 0})
        a["tickers"] += 1
        a["events"] += r["n_events"]
        a["llm_calls"] += r["llm_calls"]
        a["total_llm_s"] += r["total_llm_s"]
        a["out_tokens"] += r["out_tokens"]
        a["est_cost_usd"] += r["est_cost_usd"]
        a["errors"] += r["errors"]
        a["parse_errors"] += r["parse_errors"]
    print("\n=== SPEED / COST (aggregate) ===")
    print(f"{'provider':22} {'stocks':>6} {'events':>7} {'tok/s':>7} "
          f"{'llm_s':>8} {'cost$':>9} {'errs':>5}")
    for prov, a in agg.items():
        tps = a["out_tokens"] / a["total_llm_s"] if a["total_llm_s"] else 0
        print(f"{prov:22} {a['tickers']:>6} {a['events']:>7} {tps:>7.1f} "
              f"{a['total_llm_s']:>8.1f} {a['est_cost_usd']:>9.4f} "
              f"{a['errors'] + a['parse_errors']:>5}")


def _write_reviews(review):
    """Per-ticker markdown listing each model's events for manual grading."""
    for ticker, by_prov in review.items():
        lines = [f"# {ticker} — manual accuracy review\n",
                 "Mark each event ✅ correct / ❌ false-positive. Note any REAL "
                 "events a model MISSED at the bottom of its section. Then fill "
                 "scoring_template.csv.\n"]
        for prov, rows in by_prov.items():
            lines.append(f"\n## {prov}  ({len(rows)} events)\n")
            lines.append("| ✓/✗ | type | status | shares | price | gross_usd | date | conf | quote |")
            lines.append("|---|---|---|---|---|---|---|---|---|")
            for r in sorted(rows, key=lambda x: x["date"], reverse=True):
                lines.append(
                    f"|  | {r['type']} | {r['status']} | {r['shares']} | "
                    f"{r['price']} | {r['gross_usd']} | {r['date']} | "
                    f"{r['confidence']} | {r['quote'].replace('|', '/')} |")
            lines.append("\n_Missed real events (list here):_\n")
        (OUT_DIR / f"{ticker}__review.md").write_text("\n".join(lines))


def _write_scoring_template(rows):
    """A CSV you fill with your manual grade per (ticker, provider)."""
    path = OUT_DIR / "scoring_template.csv"
    if path.exists():
        print(f"(kept existing {path.name} — not overwriting your grades)")
        return
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "provider", "n_events",
                    "true_positives", "false_positives", "missed", "notes"])
        for r in rows:
            # pre-fill n_events; you fill TP/FP/missed after eyeballing review.md
            w.writerow([r["ticker"], r["provider"], r["n_events"], "", "", "", ""])


# ===========================================================================
# Score pass — read your filled scoring_template.csv, compute precision/recall.
# ===========================================================================
def score_pass():
    sc = OUT_DIR / "scoring_template.csv"
    if not sc.exists():
        print(f"No {sc} — run the `run` pass and grade it first.")
        return
    by_prov: dict[str, dict] = {}
    with sc.open() as f:
        for row in csv.DictReader(f):
            tp = _toint(row["true_positives"])
            fp = _toint(row["false_positives"])
            missed = _toint(row["missed"])
            if tp is None and fp is None and missed is None:
                continue  # ungraded row
            a = by_prov.setdefault(row["provider"], {"tp": 0, "fp": 0, "missed": 0, "n": 0})
            a["tp"] += tp or 0
            a["fp"] += fp or 0
            a["missed"] += missed or 0
            a["n"] += 1

    if not by_prov:
        print("No graded rows yet. Fill true_positives/false_positives/missed "
              "in scoring_template.csv.")
        return

    # merge in speed/cost from summary.csv if present
    speed = {}
    summ = OUT_DIR / "summary.csv"
    if summ.exists():
        agg: dict[str, dict] = {}
        with summ.open() as f:
            for r in csv.DictReader(f):
                g = agg.setdefault(r["provider"], {"llm_s": 0.0, "out": 0, "cost": 0.0})
                g["llm_s"] += float(r["total_llm_s"])
                g["out"] += int(r["out_tokens"])
                g["cost"] += float(r["est_cost_usd"])
        for p, g in agg.items():
            speed[p] = {"tok_s": g["out"] / g["llm_s"] if g["llm_s"] else 0,
                        "cost": g["cost"]}

    print("\n=== ACCURACY + SPEED LEADERBOARD ===")
    print(f"{'provider':22} {'prec':>6} {'recall':>7} {'F1':>6} "
          f"{'TP':>4} {'FP':>4} {'miss':>5} {'tok/s':>7} {'cost$':>8}")
    for prov, a in sorted(by_prov.items(),
                          key=lambda kv: _f1(kv[1]), reverse=True):
        prec = a["tp"] / (a["tp"] + a["fp"]) if (a["tp"] + a["fp"]) else 0
        rec = a["tp"] / (a["tp"] + a["missed"]) if (a["tp"] + a["missed"]) else 0
        f1 = _f1(a)
        sp = speed.get(prov, {})
        print(f"{prov:22} {prec:>6.2%} {rec:>7.2%} {f1:>6.2f} "
              f"{a['tp']:>4} {a['fp']:>4} {a['missed']:>5} "
              f"{sp.get('tok_s', 0):>7.1f} {sp.get('cost', 0):>8.4f}")


def _toint(s):
    s = (s or "").strip()
    return int(s) if s.isdigit() else None


def _f1(a):
    prec = a["tp"] / (a["tp"] + a["fp"]) if (a["tp"] + a["fp"]) else 0
    rec = a["tp"] / (a["tp"] + a["missed"]) if (a["tp"] + a["missed"]) else 0
    return (2 * prec * rec / (prec + rec)) if (prec + rec) else 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "run":
        run_pass()
    elif mode == "score":
        score_pass()
    else:
        print(__doc__)
        print(f"\nUnknown mode '{mode}'. Use: run | score")
