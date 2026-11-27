"""
Relevance routing.

A 20-F or prospectus can be hundreds of pages. Feeding all of it to a local
8B model is slow and hurts accuracy. So we split the Docling markdown into
sections, score each for dilution relevance (keyword weights + presence of
$/share/percent figures, with boilerplate sections penalized), and pass only
the top-K sections to the LLM. This is what keeps the whole thing laptop-fast.
"""
from __future__ import annotations

import re

from . import config

_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MONEY_RE = re.compile(r"\$\s?\d")
_SHARE_NUM_RE = re.compile(r"\d[\d,]{4,}")          # 12,345+ style share counts
_PER_SHARE_RE = re.compile(r"per\s+(?:share|unit)", re.IGNORECASE)


def split_sections(md: str) -> list[str]:
    """
    Split markdown into sections on headings; if a filing has no headings
    (common for raw HTML tables), fall back to fixed-size windows so we never
    drop content.
    """
    if _HEADING_RE.search(md):
        parts, buf = [], []
        for line in md.splitlines():
            if _HEADING_RE.match(line) and buf:
                parts.append("\n".join(buf))
                buf = [line]
            else:
                buf.append(line)
        if buf:
            parts.append("\n".join(buf))
    else:
        parts = _window(md)

    # Further split any oversized section so each chunk fits one LLM call.
    chunks: list[str] = []
    for p in parts:
        if len(p) <= config.MAX_CHUNK_CHARS:
            chunks.append(p)
        else:
            chunks.extend(_window(p))
    return [c for c in chunks if c.strip()]


def _window(text: str) -> list[str]:
    size = config.MAX_CHUNK_CHARS
    overlap = 400
    out, i = [], 0
    while i < len(text):
        out.append(text[i : i + size])
        i += size - overlap
    return out


def score_chunk(chunk: str) -> int:
    low = chunk.lower()

    # Boilerplate sections get strongly penalized.
    for hint in config.NEGATIVE_SECTION_HINTS:
        if hint in low[:200]:           # hint near the section heading
            return -100

    score = 0
    for kw, w in config.KEYWORD_WEIGHTS.items():
        if kw in low:
            score += w
    # Numeric density signals an actual offering-terms table.
    score += min(len(_MONEY_RE.findall(chunk)), 8)
    score += min(len(_SHARE_NUM_RE.findall(chunk)), 6)
    if _PER_SHARE_RE.search(chunk):
        score += 3
    # Markdown table rows are a strong signal for offering/cap tables.
    score += min(chunk.count("|---"), 5) * 2
    return score


def select_relevant(md: str, top_k: int | None = None) -> list[tuple[int, str]]:
    """Return [(score, chunk), ...] for the top-K most dilution-relevant chunks."""
    top_k = top_k or config.TOP_K_CHUNKS
    scored = [(score_chunk(c), c) for c in split_sections(md)]
    scored = [s for s in scored if s[0] > 2]      # drop clearly irrelevant
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:top_k]
