"""CSV / JSON / HTML writers for an AuditResult."""
from __future__ import annotations

import csv
import html
import json
from collections import defaultdict
from pathlib import Path

from .checks import SEVERITIES, AuditResult

HTML_ROWS_PER_CHECK = 50


def write_csv(result: AuditResult, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["severity", "check", "ticker", "cik", "message", "details_json"])
        for f in result.findings:
            w.writerow([f.severity, f.check, f.ticker or "", f.cik or "", f.message,
                        json.dumps(f.details, default=str, sort_keys=True)])
    return path


def write_json(result: AuditResult, summary_path: Path, findings_path: Path) -> tuple[Path, Path]:
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary = result.summary()
    # Small per-check samples so an API/UI can show examples without the full file.
    samples: dict[str, list] = defaultdict(list)
    for f in result.findings:
        if len(samples[f.check]) < 10:
            samples[f.check].append({"ticker": f.ticker, "cik": f.cik, "message": f.message})
    summary["samples"] = samples
    summary_path.write_text(json.dumps(summary, indent=2, default=str))
    findings_path.write_text(json.dumps([f.to_dict() for f in result.findings], default=str))
    return summary_path, findings_path


_CSS = """
body{font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;margin:0;
     background:#f7f7f8;color:#1d1d1f}
main{max-width:1100px;margin:0 auto;padding:24px 16px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}
.meta{color:#666;font-size:13px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}
.card{background:#fff;border:1px solid #e3e3e6;border-radius:8px;padding:12px 16px;min-width:140px}
.card b{display:block;font-size:24px}
table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #e3e3e6;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top}
th{background:#fafafa;font-weight:600}
.sev{font-weight:600;text-transform:uppercase;font-size:11px;padding:2px 6px;border-radius:4px}
.error{background:#fde2e1;color:#a3191b}.warning{background:#fff1cc;color:#7a5300}
.info{background:#e3effd;color:#1b4f8a}.zero{color:#999}
details{margin:6px 0}summary{cursor:pointer}
code{font-size:12px}
"""


def write_html(result: AuditResult, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    e = html.escape
    s = result.summary()
    meta = s["meta"]
    sev = s["by_severity"]
    by_check = s["by_check"]
    grouped = defaultdict(list)
    for f in result.findings:
        grouped[f.check].append(f)

    parts = [f"<!doctype html><html><head><meta charset='utf-8'>"
             f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>Data Quality Audit</title><style>{_CSS}</style></head><body><main>",
             "<h1>Data Quality Audit</h1>",
             f"<div class='meta'>DB <code>{e(str(meta['db']))}</code> &middot; as of {e(meta['as_of'])} "
             f"&middot; generated {e(meta['generated_at'])} &middot; {meta['duration_s']}s &middot; rows: "
             + ", ".join(f"{k} {v:,}" for k, v in meta["row_counts"].items()) + "</div>",
             "<div class='cards'>"]
    for level in SEVERITIES:
        parts.append(f"<div class='card'><span class='sev {level}'>{level}</span><b>{sev.get(level, 0):,}</b></div>")
    parts.append(f"<div class='card'>checks<b>{len(by_check)}</b></div></div>")

    parts.append("<h2>Checks</h2><table><tr><th>Severity</th><th>Check</th><th>What it means</th>"
                 "<th style='text-align:right'>Findings</th></tr>")
    order = {lvl: i for i, lvl in enumerate(SEVERITIES)}
    for name, info in sorted(by_check.items(), key=lambda kv: (kv[1]["count"] == 0, order[kv[1]["severity"]], kv[0])):
        cls = "" if info["count"] else " class='zero'"
        parts.append(f"<tr{cls}><td><span class='sev {info['severity']}'>{info['severity']}</span></td>"
                     f"<td><a href='#{e(name)}'>{e(name)}</a></td><td>{e(info['description'])}</td>"
                     f"<td style='text-align:right'>{info['count']:,}</td></tr>")
    parts.append("</table>")

    parts.append("<h2>Findings</h2>")
    for name, items in sorted(grouped.items(), key=lambda kv: (order[kv[1][0].severity], kv[0])):
        shown = items[:HTML_ROWS_PER_CHECK]
        more = f" (showing {len(shown)} of {len(items):,}; full list in findings.csv)" if len(items) > len(shown) else ""
        parts.append(f"<details id='{e(name)}' {'open' if items[0].severity == 'error' else ''}>"
                     f"<summary><span class='sev {items[0].severity}'>{items[0].severity}</span> "
                     f"<b>{e(name)}</b> &mdash; {len(items):,}{e(more)}</summary>"
                     "<table><tr><th>Ticker</th><th>CIK</th><th>Message</th></tr>")
        for f in shown:
            parts.append(f"<tr><td>{e(f.ticker or '')}</td><td>{e(f.cik or '')}</td><td>{e(f.message)}</td></tr>")
        parts.append("</table></details>")
    parts.append("</main></body></html>")
    path.write_text("".join(parts))
    return path


def write_all(result: AuditResult, out_dir: Path) -> dict[str, Path]:
    out_dir = Path(out_dir)
    paths = {
        "html": write_html(result, out_dir / "report.html"),
        "csv": write_csv(result, out_dir / "findings.csv"),
    }
    paths["summary_json"], paths["findings_json"] = write_json(
        result, out_dir / "summary.json", out_dir / "findings.json")
    return paths
