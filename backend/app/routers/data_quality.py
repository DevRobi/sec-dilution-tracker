"""
Serve the latest data-quality audit.

The audit itself is a batch job (``python -m app.data_quality audit``) run after
the daily sync; the API only reads the files it wrote, so requests stay cheap
and never scan the database.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

router = APIRouter(prefix="/data-quality", tags=["data-quality"])

_DEFAULT_DIR = Path(__file__).resolve().parents[2] / "reports" / "data_quality"
_cache: dict[str, tuple[float, object]] = {}


def report_dir() -> Path:
    return Path(os.getenv("DATA_QUALITY_REPORT_DIR", str(_DEFAULT_DIR)))


def _load_json(name: str):
    path = report_dir() / name
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="No data-quality report yet. Run: python -m app.data_quality audit",
        )
    mtime = path.stat().st_mtime
    hit = _cache.get(name)
    if hit and hit[0] == mtime:
        return hit[1]
    data = json.loads(path.read_text())
    _cache[name] = (mtime, data)
    return data


@router.get("/summary")
def get_summary():
    """Counts by severity and by check, run metadata, and a few sample findings per check."""
    return _load_json("summary.json")


@router.get("/findings")
def get_findings(
    check: Optional[str] = None,
    severity: Optional[str] = Query(None, pattern="^(error|warning|info)$"),
    ticker: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    findings = _load_json("findings.json")
    if check:
        findings = [f for f in findings if f["check"] == check]
    if severity:
        findings = [f for f in findings if f["severity"] == severity]
    if ticker:
        t = ticker.upper()
        findings = [f for f in findings if (f.get("ticker") or "").upper() == t]
    return {"total": len(findings), "items": findings[offset: offset + limit]}


@router.get("/report")
def get_report_html():
    path = report_dir() / "report.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="No HTML report yet.")
    return FileResponse(path, media_type="text/html")
