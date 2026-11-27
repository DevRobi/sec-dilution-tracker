"""
Pluggable LLM backends for dilution chunk extraction - Claude by default.

Providers (env DILUTION_LLM_PROVIDER, model via DILUTION_LLM_MODEL):

  claude-code  (default when ANTHROPIC_API_KEY is not set)
      Shells out to the installed Claude Code CLI (`claude -p`), which uses the
      logged-in Claude subscription instead of API credits.
  anthropic    (default when ANTHROPIC_API_KEY is set)
      Official `anthropic` Python SDK, Messages API with structured outputs.
  ollama       (explicit opt-in only)
      Local Ollama with JSON-schema constrained decoding.

All providers return the same `LLMExtraction`; `complete_text` / `complete_json`
are also used by the backend summarizer (app/services/ai).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any, Callable

from . import config
from .models import LLMExtraction
from .prompts import FEW_SHOT, SYSTEM_PROMPT

logger = logging.getLogger(__name__)

PROVIDERS = ("claude-code", "anthropic", "ollama")


class LLMError(RuntimeError):
    """Any provider failure (bad response, refusal, truncation, CLI failure)."""


class ClaudeCodeNotLoggedIn(LLMError):
    pass


# ---------------------------------------------------------------------------
# JSON schema for structured outputs
# ---------------------------------------------------------------------------
def _nullable(t: str, description: str) -> dict:
    return {"type": [t, "null"], "description": description}


# Hand-written (not pydantic's model_json_schema) so it satisfies structured
# outputs: every object has additionalProperties:false and lists all keys as
# required; optional values are expressed as nullable types instead.
EXTRACTION_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["events", "overall_confidence", "notes"],
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "offering_type", "status", "shares", "price_per_share",
                    "gross_proceeds_usd", "net_proceeds_usd", "event_date",
                    "is_approximate", "source_quote",
                ],
                "properties": {
                    "offering_type": {"type": "string", "enum": [
                        "ipo", "follow_on", "atm", "private_placement", "shelf_registration",
                        "warrant_exercise", "convertible", "equity_line", "rights_offering", "other"]},
                    "status": {"type": "string", "enum": ["completed", "announced", "registered", "proposed"]},
                    "shares": _nullable("number", "Common shares issued, offered or issuable."),
                    "price_per_share": _nullable("number", "Offering / exercise price per share in USD."),
                    "gross_proceeds_usd": _nullable("number", "Gross dollar proceeds before fees, in USD."),
                    "net_proceeds_usd": _nullable("number", "Net dollar proceeds after fees, in USD."),
                    "event_date": _nullable("string", "ISO date YYYY-MM-DD if stated."),
                    "is_approximate": {"type": "boolean"},
                    "source_quote": {"type": "string", "description": "Verbatim sentence the numbers came from."},
                },
            },
        },
        "overall_confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "notes": {"type": "string"},
    },
}


def _user_prompt(chunk: str) -> str:
    return f'{FEW_SHOT}\n\nNOW EXTRACT FROM THIS TEXT:\n"""\n{chunk}\n"""'


def _to_extraction(data: Any) -> LLMExtraction:
    if isinstance(data, str):
        data = json.loads(data)
    result = LLMExtraction.model_validate(data)
    for ev in result.events:
        ev.extraction_method = "llm"
    return result


# ---------------------------------------------------------------------------
# Provider: anthropic (API key, official SDK)
# ---------------------------------------------------------------------------
_anthropic_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic

        _anthropic_client = anthropic.Anthropic(  # reads ANTHROPIC_API_KEY
            timeout=config.LLM_TIMEOUT_SECONDS,
            max_retries=config.LLM_MAX_RETRIES,
        )
    return _anthropic_client


def _anthropic_complete(system: str, prompt: str, schema: dict | None) -> Any:
    import anthropic

    output_config: dict = {"effort": config.CLAUDE_EFFORT}
    if schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    try:
        response = _get_anthropic_client().messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=config.CLAUDE_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config=output_config,
        )
    except anthropic.RateLimitError as exc:
        raise LLMError(f"anthropic rate limited (429): {exc}") from exc
    except anthropic.APIStatusError as exc:
        raise LLMError(f"anthropic API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError(f"anthropic connection error: {exc}") from exc

    if response.stop_reason == "refusal":
        raise LLMError("anthropic refused the request (stop_reason=refusal)")
    if response.stop_reason == "max_tokens":
        raise LLMError(f"anthropic output truncated at max_tokens={config.CLAUDE_MAX_TOKENS}")
    text = "".join(b.text for b in response.content if b.type == "text")
    return json.loads(text) if schema is not None else text


# ---------------------------------------------------------------------------
# Provider: claude-code (Claude Code CLI, subscription auth)
# ---------------------------------------------------------------------------
# Env vars set by a *parent* Claude Code session; inheriting them makes the
# child CLI think it is a nested session and can break its auth.
_PARENT_SESSION_PREFIXES = ("CLAUDECODE", "CLAUDE_CODE_", "CLAUDE_AGENT_SDK_")


def _claude_bin() -> str:
    configured = config.CLAUDE_CODE_BIN
    if configured and os.path.exists(configured):
        return configured
    found = shutil.which("claude")
    if not found:
        raise LLMError("Claude Code CLI not found. Install it or set CLAUDE_CODE_BIN.")
    return found


def _claude_code_env() -> dict:
    return {k: v for k, v in os.environ.items() if not k.startswith(_PARENT_SESSION_PREFIXES)}


def _claude_code_complete(system: str, prompt: str, schema: dict | None) -> Any:
    cmd = [
        _claude_bin(), "-p",
        "--output-format", "json",
        "--model", config.CLAUDE_MODEL,
        "--tools", "",
        "--no-session-persistence",
        "--append-system-prompt", system,
    ]
    if schema is not None:
        cmd += ["--json-schema", json.dumps(schema)]
    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            timeout=config.LLM_TIMEOUT_SECONDS, env=_claude_code_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise LLMError(f"claude CLI timed out after {config.LLM_TIMEOUT_SECONDS}s") from exc

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise LLMError(
            f"claude CLI returned non-JSON (exit {proc.returncode}): {(proc.stderr or proc.stdout)[:300]}"
        ) from exc

    result = payload.get("result") or ""
    if payload.get("is_error") or proc.returncode != 0:
        if "not logged in" in str(result).lower() or "/login" in str(result):
            raise ClaudeCodeNotLoggedIn(
                "Claude Code CLI is not logged in. Run `claude` and then `/login` "
                "(or set ANTHROPIC_API_KEY and DILUTION_LLM_PROVIDER=anthropic)."
            )
        raise LLMError(f"claude CLI error (exit {proc.returncode}): {str(result)[:300]}")

    if schema is None:
        return result
    structured = payload.get("structured_output")
    if structured is not None:
        return structured
    return json.loads(result)  # fallback: some CLI versions put the JSON in result


# ---------------------------------------------------------------------------
# Provider: ollama (explicit opt-in, local)
# ---------------------------------------------------------------------------
def _ollama_complete(system: str, prompt: str, schema: dict | None) -> Any:
    import ollama

    resp = ollama.chat(
        model=config.OLLAMA_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        format=schema if schema is not None else "",
        options={"temperature": 0.0, "num_ctx": config.OLLAMA_NUM_CTX},
    )
    text = resp["message"]["content"]
    return json.loads(text) if schema is not None else text


def _ollama_available() -> bool:
    try:
        import ollama

        ollama.list()
        return True
    except Exception:  # noqa: BLE001
        return False


_BACKENDS: dict[str, Callable[[str, str, dict | None], Any]] = {
    "claude-code": _claude_code_complete,
    "anthropic": _anthropic_complete,
    "ollama": _ollama_complete,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_active_provider() -> str:
    raw = (os.getenv("DILUTION_LLM_PROVIDER") or config.DILUTION_LLM_PROVIDER or "").strip().lower()
    if raw in PROVIDERS:
        return raw
    if raw:
        logger.warning("Unknown DILUTION_LLM_PROVIDER=%r; falling back to Claude.", raw)
    return "anthropic" if os.getenv("ANTHROPIC_API_KEY") else "claude-code"


def llm_available() -> bool:
    provider = get_active_provider()
    if provider == "anthropic":
        return bool(os.getenv("ANTHROPIC_API_KEY"))
    if provider == "ollama":
        return _ollama_available()
    try:
        _claude_bin()
        return True
    except LLMError:
        return False


def complete_text(system: str, prompt: str) -> str:
    return _BACKENDS[get_active_provider()](system, prompt, None)


def complete_json(system: str, prompt: str, schema: dict) -> Any:
    return _BACKENDS[get_active_provider()](system, prompt, schema)


def get_extractor() -> Callable[[str], LLMExtraction]:
    provider = get_active_provider()
    logger.info("Using %s dilution extractor model=%s", provider,
                config.OLLAMA_MODEL if provider == "ollama" else config.CLAUDE_MODEL)
    backend = _BACKENDS[provider]
    return lambda chunk: _to_extraction(backend(SYSTEM_PROMPT, _user_prompt(chunk), EXTRACTION_SCHEMA))


def extract_with_llm(chunk: str) -> LLMExtraction:
    try:
        return get_extractor()(chunk)
    except ClaudeCodeNotLoggedIn:
        raise  # a config problem: fail loudly instead of returning zero events per chunk
    except Exception as exc:  # noqa: BLE001
        return LLMExtraction(events=[], overall_confidence="low", notes=f"llm_error: {exc!r}")
