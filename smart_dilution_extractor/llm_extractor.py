"""
LLM extraction for dilution-relevant filing chunks.

Provider selection is controlled by DILUTION_LLM_PROVIDER (see llm_providers.py):
  - claude-code: Claude Code CLI on the logged-in subscription (default without an API key)
  - anthropic:   Anthropic API via the official SDK (default when ANTHROPIC_API_KEY is set)
  - ollama:      local Ollama, explicit opt-in
"""
from __future__ import annotations

from .llm_providers import extract_with_llm, llm_available
from .models import DilutionEvent
from .prompts import FEW_SHOT, SYSTEM_PROMPT

__all__ = [
    "SYSTEM_PROMPT",
    "FEW_SHOT",
    "extract_with_llm",
    "extract_chunks",
    "llm_available",
]


def extract_chunks(scored_chunks: list[tuple[int, str]]) -> list[DilutionEvent]:
    """Run the configured LLM over scored chunks."""
    if not llm_available():
        return []
    events: list[DilutionEvent] = []
    for _score, chunk in scored_chunks:
        result = extract_with_llm(chunk)
        events.extend(result.events)
    return events
