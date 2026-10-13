"""Deterministic natural-language period resolution ("Q1 2026", "April to June", "last quarter" ...).

Why in code and not in the LLM: a wrong date silently changes a banking aggregate (and small models are
poor at calendar arithmetic). The planner LLM never emits dates; this module does, and its result is
what the executor filters on. All periods are half-open: start <= date < end.
"""
import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_M = "|".join(sorted(MONTHS, key=len, reverse=True))
_SEP = r"(?:-|–|—|to|and|through|till|until)"


@dataclass(frozen=True)
class Period:
    """A half-open date range [start, end) with the label shown to the user."""
    start: date
    end: date  # exclusive
    label: str

    @property
    def last_day(self) -> date:
        """Last calendar day inside the period (end is exclusive)."""
        return self.end - timedelta(days=1)


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    return date(y, m + 1, 1)


def month_period(year: int, month: int) -> Period:
    """The calendar month as a Period."""
    return Period(date(year, month, 1), _add_months(date(year, month, 1), 1), f"{calendar.month_name[month]} {year}")


def quarter_period(year: int, q: int) -> Period:
    """Calendar quarter q (1-4) of a year as a Period."""
    start = date(year, 3 * q - 2, 1)
    return Period(start, _add_months(start, 3), f"Q{q} {year}")


def _year_of(token: str | None, today: date) -> int:
    if not token:
        return today.year
    token = token.strip().lower()
    if token == "last year":
        return today.year - 1
    if token == "this year":
        return today.year
    return int(token)


def fiscal_year_period(today: date, fy_start_month: int, offset: int = 0) -> Period:
    """The financial year containing today (offset -1 = the previous one), for a configurable start month."""
    start_year = today.year if today.month >= fy_start_month else today.year - 1
    start = date(start_year + offset, fy_start_month, 1)
    end = _add_months(start, 12)
    label = f"FY {start.year}-{str(end.year)[-2:]}" if fy_start_month != 1 else f"FY {start.year}"
    return Period(start, end, label)


def parse_periods(text: str, today: date, fy_start_month: int = 4) -> list[Period]:
    """All periods mentioned in `text`, in order of appearance (first = the main one, second = comparison)."""
    t = text.lower()
    found: list[tuple[int, Period]] = []
    claimed: list[tuple[int, int]] = []

    def free(a: int, b: int) -> bool:
        """True if this span of the question has not already been claimed by an earlier, more specific pattern."""
        return all(b <= s or a >= e for s, e in claimed)

    def add(m: re.Match, p: Period) -> None:
        """Record a matched period and claim its text span, so 'Q1 2026' is not also read as the year 2026."""
        if free(m.start(), m.end()):
            claimed.append((m.start(), m.end()))
            found.append((m.start(), p))

    for m in re.finditer(r"\bq([1-4])\b\s*(?:of\s+)?(?:fy\s*)?(\d{4})?", t):
        add(m, quarter_period(int(m.group(2) or today.year), int(m.group(1))))

    cur_q = (today.month - 1) // 3 + 1
    for m in re.finditer(r"\b(last|previous|prior)\s+quarter\b", t):
        y, q = (today.year, cur_q - 1) if cur_q > 1 else (today.year - 1, 4)
        add(m, quarter_period(y, q))
    for m in re.finditer(r"\b(this|current)\s+quarter\b", t):
        add(m, quarter_period(today.year, cur_q))

    rng = re.compile(rf"\b(?:between\s+|from\s+)?({_M})\b\s*{_SEP}\s*({_M})\b(?:\s+(?:of\s+)?(\d{{4}}|this year|last year))?")
    for m in rng.finditer(t):
        m1, m2, year = MONTHS[m.group(1)], MONTHS[m.group(2)], _year_of(m.group(3), today)
        y1 = year - 1 if m2 < m1 else year  # e.g. "November to February" crosses a year boundary
        start, end = date(y1, m1, 1), _add_months(date(year, m2, 1), 1)
        add(m, Period(start, end, f"{calendar.month_name[m1]}–{calendar.month_name[m2]} {year}"))

    for m in re.finditer(r"\b(first|second)\s+half\b|\b(h1|h2)\b", t):
        first = (m.group(1) == "first") if m.group(1) else m.group(2) == "h1"
        tail = re.match(r"\s*(?:of\s+)?(\d{4}|this year|last year)", t[m.end():])
        year = _year_of(tail.group(1) if tail else None, today)
        start = date(year, 1 if first else 7, 1)
        a, b = m.start(), m.end() + (tail.end() if tail else 0)  # claim "of this year" too
        if free(a, b):
            claimed.append((a, b))
            found.append((a, Period(start, _add_months(start, 6), f"{'H1' if first else 'H2'} {year}")))

    for m in re.finditer(r"\b(?:last|past|previous)\s+(\d+)\s+(day|week|month|year)s?\b", t):
        n, unit = int(m.group(1)), m.group(2)
        end = today + timedelta(days=1)
        start = {"day": end - timedelta(days=n), "week": end - timedelta(weeks=n),
                 "month": _add_months(end, -n).replace(day=min(end.day, 28)), "year": date(end.year - n, end.month, min(end.day, 28))}[unit]
        add(m, Period(start, end, f"last {n} {unit}{'s' if n > 1 else ''}"))

    for m in re.finditer(r"\b(?:last|previous)\s+month\b", t):
        first = _add_months(today.replace(day=1), -1)
        add(m, month_period(first.year, first.month))
    for m in re.finditer(r"\b(?:this|current)\s+month\b", t):
        add(m, month_period(today.year, today.month))

    for m in re.finditer(r"\b(this|current|last|previous)\s+(?:financial|fiscal)\s+year\b|\bfy\b", t):
        off = -1 if (m.group(1) or "") in ("last", "previous") else 0
        add(m, fiscal_year_period(today, fy_start_month, off))
    for m in re.finditer(r"\b(?:financial|fiscal)\s+year\b", t):
        add(m, fiscal_year_period(today, fy_start_month))

    for m in re.finditer(r"\b(last|previous)\s+year\b", t):
        add(m, Period(date(today.year - 1, 1, 1), date(today.year, 1, 1), str(today.year - 1)))
    for m in re.finditer(r"\b(this|current)\s+year\b|\bytd\b|\byear to date\b", t):
        add(m, Period(date(today.year, 1, 1), date(today.year + 1, 1, 1), str(today.year)))

    for m in re.finditer(rf"\b({_M})\b\s+(\d{{4}})\b|\b(?:in|during|for|of)\s+({_M})\b(?!\s*{_SEP}\s*(?:{_M}))", t):
        month = MONTHS[m.group(1) or m.group(3)]
        add(m, month_period(int(m.group(2)) if m.group(2) else today.year, month))

    for m in re.finditer(r"\b(?:in|during|for|of)\s+(20\d\d)\b", t):
        y = int(m.group(1))
        add(m, Period(date(y, 1, 1), date(y + 1, 1, 1), str(y)))

    found.sort(key=lambda x: x[0])
    out: list[Period] = []
    for _, p in found:
        if p not in out:
            out.append(p)
    return out


def previous_period(p: Period) -> Period:
    """The equivalent period immediately before `p` (quarter -> previous quarter, month -> previous month ...)."""
    months = (p.end.year - p.start.year) * 12 + p.end.month - p.start.month
    if p.start.day == 1 and p.end.day == 1 and months in (1, 3, 6, 12):
        start = _add_months(p.start, -months)
        label = p.label
        if months == 3 and label.startswith("Q"):
            year, q0 = divmod(p.start.year * 4 + (p.start.month - 1) // 3 - 1, 4)
            label = f"Q{q0 + 1} {year}"
        elif months == 1:
            label = f"{calendar.month_name[start.month]} {start.year}"
        elif months == 12:
            label = str(start.year) if label.isdigit() else f"previous {label}"
        else:
            label = f"previous {label}"
        return Period(start, p.start, label)
    span = p.end - p.start
    return Period(p.start - span, p.start, f"previous {p.label}")
