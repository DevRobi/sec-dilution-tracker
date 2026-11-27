"""
Layout-aware parsing with Docling.

SEC filings carry the critical dilution numbers inside TABLES (offering-terms
tables, capitalization tables, the "shares outstanding after this offering"
footnote table). Naive HTML-to-text stripping shreds those tables and the LLM
then mixes up columns. Docling parses the document structure and emits clean
markdown with tables preserved as markdown pipe-tables — which both regex and
the LLM read far more reliably.

For HTML inputs Docling runs CPU-only with no ML model downloads, so a 1MB
prospectus converts in a few seconds on a laptop. Results are cached to disk.
"""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from . import config


@lru_cache(maxsize=1)
def _converter():
    # Imported lazily so importing this module is cheap.
    from docling.document_converter import DocumentConverter

    return DocumentConverter()


def to_markdown(path: Path) -> str:
    """Convert a filing document to markdown (tables preserved). Cached."""
    raw = path.read_bytes()
    digest = hashlib.sha1(raw).hexdigest()[:16]
    cache = config.CACHE_DIR / f"md_{digest}.md"
    if cache.exists():
        return cache.read_text()

    try:
        result = _converter().convert(str(path))
        md = result.document.export_to_markdown()
    except Exception as exc:  # noqa: BLE001 - fall back so pipeline never dies
        md = _fallback_text(path)
        md = f"<!-- docling failed: {exc!r}; used fallback -->\n" + md

    cache.write_text(md)
    return md


def _fallback_text(path: Path) -> str:
    """Bare-bones HTML→text if Docling can't handle a document."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(path.read_bytes(), "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text("\n")
