import pandas as pd

from app.services.securities import calculate_changes


def _df(points):
    return pd.DataFrame({"end": pd.to_datetime([d for d, _ in points]), "val": [v for _, v in points]})


def test_zero_share_point_is_never_latest():
    stats = calculate_changes(_df([("2025-03-31", 100), ("2025-06-30", 110), ("2025-09-30", 0)]))
    assert stats["latest_shares"] == 110
    assert stats["last_date"] == "2025-06-30"


def test_one_year_change_requires_point_near_one_year_back():
    # Nearest point to "1 year ago" is ~18 months back -> must NOT be used as the 1Y base.
    stats = calculate_changes(_df([("2023-06-30", 50), ("2024-12-31", 100), ("2025-12-31", 120)]))
    assert stats["change_pct_1y"] is not None  # 2024-12-31 is within the window
    stats = calculate_changes(_df([("2023-06-30", 50), ("2025-12-31", 120)]))
    assert stats["change_pct_1y"] is None
    assert stats["prev_shares_1y"] is None


def test_one_year_change_value():
    stats = calculate_changes(_df([("2024-12-31", 100), ("2025-12-31", 90)]))
    assert round(stats["change_pct_1y"], 6) == -10.0
