"""Builds a small SQLite DB (real schema from app.models) seeded with known defects."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, insert


UPDATED = datetime(2026, 4, 1)


def build_fixture_db(path: Path) -> Path:
    from app import models  # noqa: F401  (registers tables on Base)
    from app.database import Base

    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    C, H, S, O, P = (models.Company.__table__, models.History.__table__, models.Stats.__table__,
                     models.Offering.__table__, models.Price.__table__)

    with engine.begin() as cx:
        cx.execute(insert(C), [
            {"cik": "1", "ticker": "GOOD", "title": "Clean Co"},
            {"cik": "2", "ticker": "BAD", "title": "Messy Co"},
            {"cik": "3", "ticker": "OLD", "title": "Stale Co"},
            {"cik": "4", "ticker": "SHELL", "title": "Never Synced"},
            {"cik": "5", "ticker": "NOHIST", "title": "Stats Without History"},
            {"cik": "6", "ticker": "DIL", "title": "Diluter"},
        ])
        hist = []
        # GOOD: steady buyback, then a clean 2:1 forward split in 2026.
        for d, s in [("2024-12-31", 100_000_000), ("2025-03-31", 99_000_000), ("2025-06-30", 98_000_000),
                     ("2025-09-30", 97_000_000), ("2025-12-31", 96_000_000), ("2026-03-31", 192_000_000)]:
            hist.append({"cik": "1", "date": d, "shares": s, "source": "10-Q"})
        # BAD: a units error spike, a zero, a future date, a same-day conflict.
        for d, s in [("2025-03-31", 50_000_000), ("2025-06-30", 50_000), ("2025-09-30", 50_500_000),
                     ("2025-12-31", 0), ("2026-03-31", 51_000_000), ("2026-03-31", 52_000_000),
                     ("2031-01-01", 51_000_000)]:
            hist.append({"cik": "2", "date": d, "shares": s, "source": "10-Q"})
        # OLD: last filing in 2023.
        for d, s in [("2023-03-31", 10_000_000), ("2023-06-30", 10_000_000), ("2023-09-30", 10_100_000),
                     ("2023-12-31", 10_200_000)]:
            hist.append({"cik": "3", "date": d, "shares": s, "source": "10-K"})
        # DIL: genuine +80% dilution (not a split ratio).
        for d, s in [("2025-03-31", 10_000_000), ("2025-06-30", 10_500_000), ("2025-09-30", 18_900_000),
                     ("2025-12-31", 19_000_000), ("2026-03-31", 19_100_000)]:
            hist.append({"cik": "6", "date": d, "shares": s, "source": "10-Q"})
        cx.execute(insert(H), hist)

        cx.execute(insert(S), [
            {"cik": "1", "latest_shares": 192_000_000, "prev_shares_1y": 98_000_000,
             "change_pct_1y": (192 / 98 - 1) * 100, "date_1y": "2025-06-30", "last_date": "2026-03-31",
             "last_updated": UPDATED},
            {"cik": "2", "latest_shares": 0, "prev_shares_1y": 50_000_000, "change_pct_1y": 12.0,
             "date_1y": "2024-06-30", "last_date": "2026-03-31", "last_updated": UPDATED},
            {"cik": "3", "latest_shares": 10_200_000, "prev_shares_1y": 10_000_000, "change_pct_1y": 5.0,
             "date_1y": "2022-12-31", "last_date": "2023-12-31", "last_updated": UPDATED},
            {"cik": "5", "latest_shares": 1_000_000, "prev_shares_1y": None, "change_pct_1y": None,
             "date_1y": None, "last_date": None, "last_updated": UPDATED},
            {"cik": "6", "latest_shares": 19_100_000, "prev_shares_1y": 10_500_000,
             "change_pct_1y": (19.1 / 10.5 - 1) * 100, "date_1y": "2025-06-30", "last_date": "2026-03-31",
             "last_updated": UPDATED},
        ])

        acc1, acc2, acc3 = "0000000002-25-000001", "0000000002-25-000002", "0000000002-26-000003"
        off = [
            # same instrument stored twice (different fingerprint suffix)
            dict(id=1, cik="2", date="2025-07-01", category="Warrants / SAFEs", source_filing=f"424B5:{acc1}:aaaaaaaa",
                 amount=None, amount_text="1,000,000 shares", status="Potential",
                 text="1,000,000 shares issuable upon exercise of warrants",
                 description="Warrants/options — 1,000,000 shares issuable, $2.0000/sh"),
            dict(id=2, cik="2", date="2025-07-01", category="Warrants / SAFEs", source_filing=f"424B5:{acc1}:bbbbbbbb",
                 amount=None, amount_text="1,000,000 shares", status="Potential",
                 text="1,000,000 shares issuable upon exercise of warrants",
                 description="Warrants/options — 1,000,000 shares issuable, $2.0000/sh"),
            # LLM digit misread: quote says 2,349,675
            dict(id=3, cik="2", date="2025-07-01", category="Warrants / SAFEs", source_filing=f"424B5:{acc1}:cccccccc",
                 amount=None, amount_text="2,349,675 shares", status="Potential",
                 text="2,349,675 shares issuable upon exercise of warrants at $2.36",
                 description="Warrants/options — 2,349,675 shares issuable, $2.3600/sh"),
            dict(id=4, cik="2", date="2025-07-01", category="Warrants / SAFEs", source_filing=f"424B5:{acc1}:dddddddd",
                 amount=None, amount_text="2,339,675 shares", status="Potential",
                 text="2,349,675 shares issuable upon exercise of warrants at $2.36",
                 description="Warrants/options — 2,339,675 shares issuable, $2.3600/sh"),
            # the July raise, then recapped in a September prospectus
            dict(id=5, cik="2", date="2025-07-01", category="Series / Direct", source_filing=f"424B5:{acc1}:eeeeeeee",
                 amount=7_500_000.0, amount_text="$7.5M", status="Active",
                 text="sale by us of 7,143,000 shares at $1.05", description="Follow-on offering — 7,143,000 shares"),
            dict(id=6, cik="2", date="2025-09-15", category="Series / Direct", source_filing=f"424B5:{acc2}:ffffffff",
                 amount=None, amount_text="7,143,000 shares", status="Active",
                 text="sale of 7,143,000 shares in July 2025", description="Follow-on offering — 7,143,000 shares"),
            # zero amount, incomplete row, shelf marked active
            dict(id=7, cik="2", date="2025-09-15", category="Series / Direct", source_filing=f"424B5:{acc2}:11111111",
                 amount=0.0, amount_text=None, status="Active", text="", description=""),
            dict(id=8, cik="2", date="2025-09-15", category="Common Stock", source_filing=f"424B5:{acc2}:22222222",
                 amount=None, amount_text=None, status="Active", text="", description=""),
            # $10M shelf, $15M of takedowns -> overdrawn
            dict(id=9, cik="6", date="2025-01-01", category="Shelf Offering", source_filing=f"S-3:{acc3}:33333333",
                 amount=10_000_000.0, amount_text="$10.0M", status="Potential", text="", description=""),
            dict(id=10, cik="6", date="2025-06-01", category="Series / Direct", source_filing=f"424B5:{acc3}:44444444",
                 amount=9_000_000.0, amount_text="$9.0M", status="Active", text="", description=""),
            dict(id=11, cik="6", date="2025-08-01", category="Series / Direct", source_filing=f"424B5:{acc2}:55555555",
                 amount=6_000_000.0, amount_text="$6.5M", status="Active", text="", description=""),
            dict(id=12, cik="6", date="2025-08-01", category="Shelf Offering", source_filing=f"424B5:{acc2}:66666666",
                 amount=1.0, amount_text=None, status="Active", text="", description=""),
        ]
        cx.execute(insert(O), off)
        cx.execute(insert(P), [
            {"cik": "1", "date": "2026-04-01T20:00:00", "price": 10.0},
            {"cik": "2", "date": "2026-03-01T20:00:00", "price": 1.0},
            {"cik": "6", "date": "2026-04-01T20:00:00", "price": 3.0},
        ])
    engine.dispose()
    return path
