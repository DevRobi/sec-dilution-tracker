"""
Data-quality audit for the dilution tracker database.

Answers the question a data consumer should ask before trusting a ranking:
"did today's feed arrive, is it complete, and do the numbers add up?"

    python -m app.data_quality audit --db tracker.db --out reports/data_quality

The audit opens SQLite strictly read-only (``mode=ro`` URI) and never writes to
the database. See ``checks.py`` for the individual checks and ``report.py`` for
the CSV / JSON / HTML writers.
"""
from .checks import AuditConfig, AuditResult, Finding, run_audit  # noqa: F401
