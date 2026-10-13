from datetime import date

from app.rag.periods import parse_periods, previous_period

TODAY = date(2026, 10, 12)  # Q4 2026


def one(text):
    ps = parse_periods(text, TODAY)
    assert len(ps) == 1, ps
    return ps[0]


def test_explicit_quarter():
    p = one("How much loan was disbursed in Q1 2026?")
    assert (p.start, p.end, p.label) == (date(2026, 1, 1), date(2026, 4, 1), "Q1 2026")


def test_last_and_this_quarter():
    assert one("spent more than 10000 last quarter").start == date(2026, 7, 1)
    assert one("NPA accounts this quarter").start == date(2026, 10, 1)


def test_last_quarter_in_january_wraps_year():
    p = parse_periods("last quarter", date(2026, 1, 15))[0]
    assert (p.start, p.end) == (date(2025, 10, 1), date(2026, 1, 1))


def test_month_range_this_year():
    p = one("total transactions between April and June this year")
    assert (p.start, p.end) == (date(2026, 4, 1), date(2026, 7, 1))


def test_month_range_with_year_and_abbreviations():
    p = one("sum from Jan to Mar 2025")
    assert (p.start, p.end) == (date(2025, 1, 1), date(2025, 4, 1))


def test_month_range_crossing_year_boundary():
    p = one("november to february 2026")
    assert (p.start, p.end) == (date(2025, 11, 1), date(2026, 3, 1))


def test_first_half():
    p = one("average monthly spending in the first half of this year")
    assert (p.start, p.end) == (date(2026, 1, 1), date(2026, 7, 1))


def test_last_n_months_is_rolling():
    p = one("top 3 categories for the last 3 months")
    assert p.end == date(2026, 10, 13) and p.start == date(2026, 7, 13)


def test_financial_year_april_start():
    p = one("month-wise trend of disbursements for the current financial year")
    assert (p.start, p.end) == (date(2026, 4, 1), date(2027, 4, 1))


def test_last_month_and_named_month():
    assert one("last month").start == date(2026, 9, 1)
    assert one("what did I spend in March 2026").start == date(2026, 3, 1)


def test_two_periods_keep_order_for_comparison():
    ps = parse_periods("Which branch disbursed the most in Q1 2026 and how does it compare to Q4 2025?", TODAY)
    assert [p.label for p in ps] == ["Q1 2026", "Q4 2025"]


def test_no_period():
    assert parse_periods("what is the home loan rate", TODAY) == []
    assert parse_periods("may I know my balance", TODAY) == []  # "may" is not the month


def test_previous_period_aligned_and_label():
    p = previous_period(parse_periods("Q1 2026", TODAY)[0])
    assert (p.start, p.end, p.label) == (date(2025, 10, 1), date(2026, 1, 1), "Q4 2025")
    m = previous_period(parse_periods("in March 2026", TODAY)[0])
    assert (m.start, m.label) == (date(2026, 2, 1), "February 2026")
