"""
Backend entry point for LLM calls (legacy offering scanner + summaries).

Delegates to smart_dilution_extractor.llm_providers so the web app and the AI
worker share one provider switch (DILUTION_LLM_PROVIDER = claude-code |
anthropic | ollama; default Claude Haiku 5.5 via DILUTION_LLM_MODEL). The
provider calls are blocking (SDK request or `claude -p` subprocess), so they run
in a worker thread to keep the event loop free.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# The extractor package lives at the repo root, next to backend/.
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.append(str(_REPO_ROOT))  # append: never shadow the backend `app` package

from smart_dilution_extractor import llm_providers  # noqa: E402
from smart_dilution_extractor.llm_providers import LLMError  # noqa: E402,F401


def active_provider() -> str:
    return llm_providers.get_active_provider()


async def complete_text(system: str, prompt: str) -> str:
    return await asyncio.to_thread(llm_providers.complete_text, system, prompt)


async def complete_json(system: str, prompt: str, schema: dict) -> Any:
    return await asyncio.to_thread(llm_providers.complete_json, system, prompt, schema)
