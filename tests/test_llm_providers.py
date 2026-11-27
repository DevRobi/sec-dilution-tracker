"""Offline tests for the Claude provider layer (SDK and CLI are mocked)."""
import asyncio
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import anthropic
import httpx2
import pytest

from smart_dilution_extractor import config, llm_providers as lp

EVENT = {
    "offering_type": "follow_on", "status": "announced", "shares": 1_000_000, "price_per_share": 2.0,
    "gross_proceeds_usd": 2_000_000, "net_proceeds_usd": None, "event_date": None,
    "is_approximate": False, "source_quote": "We are offering 1,000,000 shares at $2.00 per share.",
}
PAYLOAD = {"events": [EVENT], "overall_confidence": "high", "notes": ""}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DILUTION_LLM_PROVIDER", raising=False)
    monkeypatch.setattr(config, "DILUTION_LLM_PROVIDER", "")
    lp._anthropic_client = None


# --- provider selection -------------------------------------------------------
def test_default_is_claude_code_without_api_key():
    assert lp.get_active_provider() == "claude-code"
    assert config.CLAUDE_MODEL == "claude-haiku-5-5"


def test_default_is_anthropic_with_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert lp.get_active_provider() == "anthropic"


@pytest.mark.parametrize("value,expected", [("ollama", "ollama"), ("anthropic", "anthropic"),
                                            ("CLAUDE-CODE", "claude-code"), ("deepseek", "claude-code")])
def test_explicit_provider(monkeypatch, value, expected):
    monkeypatch.setenv("DILUTION_LLM_PROVIDER", value)
    assert lp.get_active_provider() == expected


def test_schema_is_strict():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(lp.EXTRACTION_SCHEMA)


# --- anthropic SDK provider -----------------------------------------------------
def _response(text, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason,
                           content=[SimpleNamespace(type="thinking", thinking=""),
                                    SimpleNamespace(type="text", text=text)])


def _client(response=None, side_effect=None):
    client = MagicMock()
    client.messages.create.return_value = response
    client.messages.create.side_effect = side_effect
    return client


def test_anthropic_structured_call_shape(monkeypatch):
    monkeypatch.setenv("DILUTION_LLM_PROVIDER", "anthropic")
    client = _client(_response(json.dumps(PAYLOAD)))
    with patch.object(lp, "_get_anthropic_client", return_value=client):
        result = lp.extract_with_llm("chunk text")
    assert result.events[0].shares == 1_000_000 and result.events[0].extraction_method == "llm"
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-haiku-5-5"
    assert kwargs["max_tokens"] == 16000
    assert kwargs["output_config"]["effort"] == "low"
    assert kwargs["output_config"]["format"] == {"type": "json_schema", "schema": lp.EXTRACTION_SCHEMA}
    assert kwargs["messages"][-1]["role"] == "user"  # no assistant prefill
    for banned in ("temperature", "top_p", "top_k", "thinking"):
        assert banned not in kwargs


def test_anthropic_text_call_has_no_format(monkeypatch):
    monkeypatch.setenv("DILUTION_LLM_PROVIDER", "anthropic")
    client = _client(_response("A short summary."))
    with patch.object(lp, "_get_anthropic_client", return_value=client):
        assert lp.complete_text("sys", "prompt") == "A short summary."
    assert "format" not in client.messages.create.call_args.kwargs["output_config"]


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_anthropic_bad_stop_reasons_raise(monkeypatch, stop_reason):
    monkeypatch.setenv("DILUTION_LLM_PROVIDER", "anthropic")
    with patch.object(lp, "_get_anthropic_client", return_value=_client(_response("{}", stop_reason))):
        with pytest.raises(lp.LLMError, match=stop_reason):
            lp.complete_json("sys", "p", {"type": "object"})
        # extract_with_llm degrades to zero events with a note instead of crashing the pipeline
        assert "llm_error" in lp.extract_with_llm("chunk").notes


def _status_error(cls, code):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(message=f"err {code}", response=httpx2.Response(code, request=req), body=None)


@pytest.mark.parametrize("exc", [
    _status_error(anthropic.RateLimitError, 429),
    _status_error(anthropic.InternalServerError, 500),
    anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")),
])
def test_anthropic_api_errors_wrapped(monkeypatch, exc):
    monkeypatch.setenv("DILUTION_LLM_PROVIDER", "anthropic")
    with patch.object(lp, "_get_anthropic_client", return_value=_client(side_effect=exc)):
        with pytest.raises(lp.LLMError):
            lp.complete_text("sys", "p")


# --- claude-code CLI provider ----------------------------------------------------
def _proc(payload, returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=json.dumps(payload), stderr="")


def test_claude_code_structured_output(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")  # parent-session vars must not leak into the child
    monkeypatch.setattr(lp, "_claude_bin", lambda: "/usr/local/bin/claude")
    out = {"type": "result", "is_error": False, "result": "", "structured_output": PAYLOAD}
    with patch.object(lp.subprocess, "run", return_value=_proc(out)) as run:
        result = lp.extract_with_llm("chunk text")
    assert result.events[0].price_per_share == 2.0
    cmd = run.call_args.args[0]
    assert cmd[:4] == ["/usr/local/bin/claude", "-p", "--output-format", "json"]
    assert cmd[cmd.index("--model") + 1] == "claude-haiku-5-5"
    assert cmd[cmd.index("--tools") + 1] == ""
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == lp.EXTRACTION_SCHEMA
    assert "--append-system-prompt" in cmd
    kw = run.call_args.kwargs
    assert "chunk text" in kw["input"] and kw["timeout"] == config.LLM_TIMEOUT_SECONDS
    assert "CLAUDECODE" not in kw["env"]


def test_claude_code_text_and_result_fallback(monkeypatch):
    monkeypatch.setattr(lp, "_claude_bin", lambda: "claude")
    with patch.object(lp.subprocess, "run", return_value=_proc({"is_error": False, "result": "Summary."})) as run:
        assert lp.complete_text("s", "p") == "Summary."
    assert "--json-schema" not in run.call_args.args[0]
    with patch.object(lp.subprocess, "run", return_value=_proc({"is_error": False, "result": json.dumps(PAYLOAD)})):
        assert lp.complete_json("s", "p", lp.EXTRACTION_SCHEMA) == PAYLOAD


def test_claude_code_not_logged_in(monkeypatch):
    monkeypatch.setattr(lp, "_claude_bin", lambda: "claude")
    out = {"type": "result", "is_error": True, "result": "Not logged in · Please run /login"}
    with patch.object(lp.subprocess, "run", return_value=_proc(out, returncode=1)):
        with pytest.raises(lp.ClaudeCodeNotLoggedIn, match="/login"):
            lp.extract_with_llm("chunk")


def test_claude_code_timeout_and_garbage(monkeypatch):
    monkeypatch.setattr(lp, "_claude_bin", lambda: "claude")
    with patch.object(lp.subprocess, "run", side_effect=subprocess.TimeoutExpired("claude", 1)):
        with pytest.raises(lp.LLMError, match="timed out"):
            lp.complete_text("s", "p")
    bad = subprocess.CompletedProcess(args=[], returncode=2, stdout="oops", stderr="boom")
    with patch.object(lp.subprocess, "run", return_value=bad):
        with pytest.raises(lp.LLMError, match="non-JSON"):
            lp.complete_text("s", "p")


def test_llm_available(monkeypatch):
    monkeypatch.setattr(lp, "_claude_bin", lambda: "claude")
    assert lp.llm_available()
    monkeypatch.setenv("DILUTION_LLM_PROVIDER", "anthropic")
    assert not lp.llm_available()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert lp.llm_available()


# --- backend services use the same layer --------------------------------------------
def test_backend_summarizer_and_extractor(monkeypatch):
    from app.services.ai import extraction, summary

    async def fake_text(system, prompt):
        assert "SIDU" in prompt
        return "  Analyst paragraph.  "

    async def fake_json(system, prompt, schema):
        assert schema is extraction.EVENTS_SCHEMA
        return {"events": [{"category": "ATM Offering", "amount": 5e7, "amount_text": "$50.0 million",
                            "status": "Active", "description": "ATM.", "source_filing": "8-K"}]}

    monkeypatch.setattr(summary, "complete_text", fake_text)
    monkeypatch.setattr(extraction, "complete_json", fake_json)
    assert asyncio.run(summary.DilutionSummarizer().summarize("SIDU", [{"x": 1}])) == "Analyst paragraph."
    assert asyncio.run(summary.DilutionSummarizer().summarize("SIDU", [])) == summary.NO_EVENTS
    events = asyncio.run(extraction.DilutionExtractor().extract("x" * 200, "8-K"))
    assert events[0]["amount"] == 5e7
