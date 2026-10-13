import pandas as pd

from app.rag.analytics import series_trends


def _rates():
    days = pd.date_range("2026-09-01", periods=10).strftime("%Y-%m-%d")
    rows = []
    for i, d in enumerate(days):
        rows.append({"date": d, "product": "FD 1 year", "rate_pct": str(6.5 + i * 0.01), "source": "card"})
        rows.append({"date": d, "product": "FD 3 year", "rate_pct": "7.0", "source": "card"})
        rows.append({"date": d, "product": "Savings", "rate_pct": "3.0", "source": "card"})
    return pd.DataFrame(rows)


def test_matches_only_requested_series_and_computes_change():
    out = series_trends(_rates(), "How has the 1-year FD rate changed over the last 30 days?")
    assert [o["series"] for o in out] == ["FD 1 year"]
    assert out[0]["start_value"] == 6.5 and out[0]["end_value"] == 6.59 and out[0]["change"] == 0.09


def test_window_limits_observations():
    out = series_trends(_rates(), "savings rate last 3 days")
    assert out[0]["series"] == "Savings" and out[0]["observations"] == 4


def test_unmatched_question_returns_all_series():
    assert len(series_trends(_rates(), "how have rates moved")) == 3


def test_non_series_table_is_ignored():
    assert series_trends(pd.DataFrame({"product": ["a"], "tenure": ["5"]}), "x") == []
