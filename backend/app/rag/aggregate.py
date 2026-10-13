"""Exact aggregation over the complete Excel/CSV tables ("structured compute", not embedding retrieval).

Why: top-K similarity search cannot answer "total disbursed in Q1" - the answer needs *every* row in
a date range. So the flow is:

    question --(LLM planner)--> AggPlan  --(this module, pandas)--> exact numbers --> UI / reply text

* The LLM only chooses table, columns, filters, grouping and metrics (a small typed AggPlan). It never
  sees rows and never does arithmetic. Dates are resolved by `periods.py`, not by the model.
* Every column/table name in the plan is validated against the real schema (fuzzy-matched or rejected).
* Guardrails: an empty match, a period outside the data's date coverage, or an invalid plan produces an
  explicit message - the system never falls back to guessing a figure.
* Cross-file questions use `join` (left join on a key, right side de-duplicated) or `denominator`
  (a second sub-query over another file, e.g. recovered / total outstanding).
"""
import difflib
import re
from dataclasses import dataclass, field
from typing import Literal

import pandas as pd
from pydantic import BaseModel, Field

from .periods import Period, previous_period
from .tables import to_number

TIME_KEYS = ("day", "month", "quarter", "year")
MONEY_HINT = re.compile(r"amount|inr|principal|outstanding|emi|balance|fee|disburs|recover|income|value|spend|total", re.I)


class PlanError(ValueError):
    """The plan cannot be executed exactly (unknown table/column, bad filter ...). Reported, never guessed around."""


# ------------------------------------------------------------------ plan schema (what the LLM fills in)


class Filter(BaseModel):
    column: str
    op: Literal["=", "!=", ">", ">=", "<", "<=", "contains", "in"]
    value: str = Field(description="For 'in', a comma-separated list. Use values exactly as listed in the table description.")


class Metric(BaseModel):
    op: Literal["sum", "count", "count_distinct", "avg", "min", "max"]
    column: str | None = Field(None, description="null for a plain row count")


class Join(BaseModel):
    table: str
    left_on: str
    right_on: str


class SubQuery(BaseModel):
    table: str = Field(description="Exact table name from the catalog")
    date_column: str | None = Field(None, description="Date column to apply the question's period to")
    filters: list[Filter] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list, description="Columns, or the derived keys month | quarter | year (of date_column)")
    metrics: list[Metric] = Field(default_factory=list)
    join: Join | None = Field(None, description="Bring in columns from a second table (e.g. branch from accounts)")
    top_n: int | None = Field(None, description="Keep only the top N groups by the first metric")
    sort_desc: bool = True
    group_stat: Literal["none", "mean_of_groups"] = Field("none", description="mean_of_groups = average of the per-group totals (e.g. average MONTHLY spend)")
    share_of_total: bool = Field(False, description="Add each group's percentage of the total")
    top_rows: int | None = Field(None, description="Also list the N largest individual rows by top_rows_by")
    top_rows_by: str | None = None


class AggPlan(BaseModel):
    is_aggregate: bool = Field(description="True if the question needs totals/counts/averages/rankings/trends over many rows")
    query: SubQuery | None = None
    denominator: SubQuery | None = Field(None, description="Second query whose total divides the first (percentage of ...)")
    denominator_uses_period: bool = False
    compare_previous_period: bool = Field(False, description="Compare with another period named in the question (or the one before)")


# ------------------------------------------------------------------ schema helpers


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def resolve_column(df: pd.DataFrame, name: str) -> str:
    """Exact -> case/punctuation-insensitive -> close match. Anything else is a PlanError (no silent guessing)."""
    cols = list(df.columns)
    if name in cols:
        return name
    by_norm = {_norm(c): c for c in cols}
    if _norm(name) in by_norm:
        return by_norm[_norm(name)]
    close = difflib.get_close_matches(_norm(name), list(by_norm), n=1, cutoff=0.8)
    if close:
        return by_norm[close[0]]
    raise PlanError(f"column '{name}' does not exist (available: {', '.join(cols[:25])})")


def resolve_table(tables: dict[str, pd.DataFrame], name: str) -> tuple[str, pd.DataFrame]:
    if name in tables:
        return name, tables[name]
    low = {k.lower(): k for k in tables}
    if name.lower() in low:
        return low[name.lower()], tables[low[name.lower()]]
    close = difflib.get_close_matches(name.lower(), list(low), n=1, cutoff=0.6)
    if close:
        return low[close[0]], tables[low[close[0]]]
    hits = [k for k in tables if _norm(name) in _norm(k)]
    if len(hits) == 1:
        return hits[0], tables[hits[0]]
    raise PlanError(f"table '{name}' is not among the indexed files (available: {', '.join(tables)})")


_CLEAN_NUM = re.compile(r"^\s*(?:rs\.?|inr|₹)?\s*(-?\d[\d,]*(?:\.\d+)?)\s*(k|lakh|lakhs|lac|crore|crores|cr|m|million)?\s*$", re.I)
_MULT = {"k": 1e3, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "crore": 1e7, "crores": 1e7, "cr": 1e7, "m": 1e6, "million": 1e6}


def parse_amount(text: str) -> float | None:
    """Strict: '10,000', 'Rs. 10000', '₹5 lakh', '2cr' -> number. Anything else (a sentence, a range) -> None."""
    m = _CLEAN_NUM.match(text)
    return float(m.group(1).replace(",", "")) * _MULT.get((m.group(2) or "").lower(), 1) if m else None


def amounts_in(question: str) -> list[float]:
    """Amounts mentioned in the question (given to the planner so it never has to transcribe them)."""
    found = re.findall(r"(?:rs\.?|inr|₹)\s*[\d,]+(?:\.\d+)?(?:\s*(?:k|lakh|lakhs|lac|crore|crores|cr|m|million)\b)?|\b\d[\d,]*(?:\.\d+)?\s*(?:k|lakh|lakhs|lac|crore|crores|cr)\b|\b\d{1,3}(?:,\d{3})+\b|\b\d{4,}\b", question, re.I)
    vals = []
    for f in found:
        v = parse_amount(f)
        if v is not None and not (re.fullmatch(r"\d{4}", f.strip()) and 1990 <= v <= 2100):  # a bare year is not an amount
            vals.append(v)
    return vals


def _nums(s: pd.Series) -> pd.Series:
    return s.map(to_number).astype("float64")


def _dates(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce", utc=True).dt.tz_localize(None)


def _is_numeric(s: pd.Series) -> bool:
    vals = s[s.astype(str).str.strip() != ""]
    return len(vals) > 0 and vals.head(300).map(to_number).notna().mean() >= 0.8


def _is_date(s: pd.Series) -> bool:
    vals = s[s.astype(str).str.strip() != ""].head(100)
    return len(vals) > 0 and vals.astype(str).str.match(r"^\d{4}-\d{2}-\d{2}|^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}").mean() >= 0.8


def guess_date_column(df: pd.DataFrame) -> str | None:
    named = [c for c in df.columns if re.search(r"date|time|_on$|_at$", c, re.I) and _is_date(df[c])]
    return named[0] if named else next((c for c in df.columns if _is_date(df[c])), None)


# ------------------------------------------------------------------ execution


@dataclass
class ExecResult:
    df: pd.DataFrame
    keys: list[str]
    metrics: list[str]
    rows_matched: int
    rows_in_table: int
    coverage: tuple[str, str] | None
    scalars: dict[str, float] = field(default_factory=dict)
    group_mean: float | None = None
    detail: pd.DataFrame | None = None
    notes: list[str] = field(default_factory=list)
    empty_hint: str = ""


def metric_name(m: Metric) -> str:
    return "count" if m.op == "count" and not m.column else f"{m.op}_{m.column}"


def _apply_filter(df: pd.DataFrame, f: Filter) -> pd.DataFrame:
    col = resolve_column(df, f.column)
    s, v = df[col], f.value.strip()
    if f.op in (">", ">=", "<", "<="):
        if _is_numeric(s):
            right = parse_amount(v)
            if right is None:
                raise PlanError(f"filter value '{v[:60]}' for numeric column '{col}' must be a plain number (e.g. 10000)")
            left = _nums(s)
        elif _is_date(s):
            left, right = _dates(s), pd.to_datetime(v, errors="coerce")
            if pd.isna(right):
                raise PlanError(f"cannot compare {col} with '{v}'")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) and f.op in ("<=", ">"):
                right = right + pd.Timedelta(days=1)  # a date-only bound means the whole day: "<= 2026-10-12" includes its afternoon rows
                f = Filter(column=f.column, op="<" if f.op == "<=" else ">=", value=v)
        else:
            raise PlanError(f"column '{col}' is not numeric or a date; cannot use '{f.op}'")
        mask = {">": left > right, ">=": left >= right, "<": left < right, "<=": left <= right}[f.op]
    elif f.op in ("=", "!="):
        if _is_numeric(s) and parse_amount(v) is not None:
            mask = _nums(s) == parse_amount(v)
        else:
            mask = s.astype(str).str.strip().str.casefold() == v.casefold()
        mask = mask if f.op == "=" else ~mask
    elif f.op == "contains":
        mask = s.astype(str).str.contains(re.escape(v), case=False, na=False)
    else:  # in
        wanted = {x.strip().casefold() for x in v.split(",") if x.strip()}
        mask = s.astype(str).str.strip().str.casefold().isin(wanted)
    return df[mask.fillna(False)]


def _hint_values(df: pd.DataFrame, filters: list[Filter]) -> str:
    """When a filter matched nothing, show the real values of that column so the user can rephrase."""
    parts = []
    for f in filters:
        try:
            col = resolve_column(df, f.column)
        except PlanError:
            continue
        uniq = [u for u in df[col].astype(str).unique() if u.strip()]
        if 0 < len(uniq) <= 20 and not _is_numeric(df[col]):
            parts.append(f"{col} has values: {', '.join(uniq)}")
    return "; ".join(parts)


def _key_series(df: pd.DataFrame, name: str, dates: pd.Series | None) -> tuple[str, pd.Series]:
    low = name.lower()
    if low in TIME_KEYS:
        if dates is None:
            raise PlanError(f"cannot group by {low}: no date column available")
        if low == "day":
            return low, dates.dt.strftime("%Y-%m-%d")
        if low == "month":
            return low, dates.dt.strftime("%Y-%m")
        if low == "quarter":
            return low, dates.dt.year.astype("Int64").astype(str) + "-Q" + dates.dt.quarter.astype("Int64").astype(str)
        return low, dates.dt.year.astype("Int64").astype(str)
    col = resolve_column(df, name)
    return col, df[col].astype(str).str.strip()


def _month_index(period: Period | None, present: pd.Series, data_end: str | None = None) -> list[str]:
    """Every month of the period (empty ones included), but not months after the last date in the data."""
    if period is not None:
        months = [d.strftime("%Y-%m") for d in pd.period_range(period.start, period.last_day, freq="M")]
        return [m for m in months if not data_end or m <= data_end[:7]]
    return sorted(present.unique())


def execute(sub: SubQuery, tables: dict[str, pd.DataFrame], period: Period | None, *,
            only_groups: pd.DataFrame | None = None, apply_top: bool = True) -> ExecResult:
    """Run one SubQuery exactly. `only_groups` restricts to the given key combinations (used for comparisons)."""
    notes: list[str] = []
    tname, df = resolve_table(tables, sub.table)
    rows_in_table = len(df)
    if sub.join:
        jname, right = resolve_table(tables, sub.join.table)
        l_on, r_on = resolve_column(df, sub.join.left_on), resolve_column(right, sub.join.right_on)
        if right[r_on].duplicated().any():
            notes.append(f"{jname} has repeated {r_on} values; the first row per key was used for the join")
            right = right.drop_duplicates(r_on)
        df = df.merge(right, how="left", left_on=l_on, right_on=r_on, suffixes=("", "_" + jname.split(".")[0]))

    needs_dates = bool(period) or any(g.lower() in TIME_KEYS for g in sub.group_by)
    date_col = resolve_column(df, sub.date_column) if sub.date_column else (guess_date_column(df) if needs_dates else None)
    if needs_dates and not date_col:
        raise PlanError(f"table '{tname}' has no date column to apply the period / time grouping to")
    dates = _dates(df[date_col]) if date_col else None

    coverage = None
    if dates is not None and dates.notna().any():
        coverage = (f"{dates.min():%Y-%m-%d}", f"{dates.max():%Y-%m-%d}")
    if period is not None:
        mask = (dates >= pd.Timestamp(period.start)) & (dates < pd.Timestamp(period.end))
        if coverage and (period.start < pd.Timestamp(coverage[0]).date() or period.last_day > pd.Timestamp(coverage[1]).date()):
            notes.append(f"{period.label} extends beyond the data, which covers {coverage[0]} to {coverage[1]}; figures reflect available data only")
        df, dates = df[mask], dates[mask]

    for f in sub.filters:
        if period is not None and date_col and resolve_column(df, f.column) == date_col:
            notes.append(f"the period {period.label} was applied by the system; the model's extra filter on {date_col} was ignored")
            continue
        df = _apply_filter(df, f)
    dates = dates.loc[df.index] if dates is not None else None
    if "status" in [c.lower() for c in df.columns] and not any(f.column.lower() == "status" for f in sub.filters):
        notes.append("no status filter applied: all statuses are included")

    if df.empty:
        hint = _hint_values(tables[tname], sub.filters)
        return ExecResult(pd.DataFrame(), [], [], 0, rows_in_table, coverage, notes=notes, empty_hint=hint)

    metrics = sub.metrics or [Metric(op="count")]
    work = pd.DataFrame(index=df.index)
    key_names: list[str] = []
    for g in sub.group_by:
        kname, ser = _key_series(df, g, dates)
        if kname not in TIME_KEYS and len(df) > 20 and ser.nunique() > 0.5 * len(df):
            raise PlanError(f"grouping by '{kname}' (almost every row is unique) makes no sense; remove that group_by or use a categorical column")
        work[kname] = ser
        key_names.append(kname)
    if key_names and any(k in TIME_KEYS for k in key_names):
        keep = dates.notna()
        work, df, dates = work[keep], df[keep], dates[keep]
    names = []
    for i, m in enumerate(metrics):
        nm = metric_name(m)
        names.append(nm)
        if m.column is None:
            work[f"__m{i}"] = 1
        else:
            col = resolve_column(df, m.column)
            if m.op in ("sum", "avg", "min", "max"):
                if not _is_numeric(df[col]):
                    raise PlanError(f"column '{col}' is not numeric; cannot compute {m.op}")
                work[f"__m{i}"] = _nums(df[col])
            else:
                work[f"__m{i}"] = df[col].astype(str).str.strip().where(df[col].astype(str).str.strip() != "")
    if only_groups is not None and key_names:
        wanted = only_groups[key_names].drop_duplicates()
        in_groups = work[key_names].reset_index(drop=True).merge(wanted, how="left", indicator=True)["_merge"].eq("both").values
        work = work[in_groups]
        if work.empty:  # the group(s) had no rows in this period (e.g. the top branch did not exist last quarter)
            return ExecResult(pd.DataFrame(), key_names, names, 0, rows_in_table, coverage, notes=notes)

    fn = {"sum": "sum", "count": "count", "count_distinct": "nunique", "avg": "mean", "min": "min", "max": "max"}

    def agg(frame: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
        grp = frame.assign(__all=1).groupby(keys or ["__all"], sort=True, dropna=True)
        out = pd.concat([getattr(grp[f"__m{i}"], fn[m.op])().rename(nm) for i, (m, nm) in enumerate(zip(metrics, names))], axis=1)
        out = out.reset_index()
        return out.drop(columns="__all") if not keys else out

    res = agg(work, key_names)
    scalars: dict[str, float] = {}
    if key_names:
        total = agg(work, [])
        for m, nm in zip(metrics, names):
            if m.op in ("sum", "count", "count_distinct", "avg", "min", "max"):
                scalars[nm] = float(total.iloc[0][nm])
    else:
        scalars = {nm: float(res.iloc[0][nm]) for nm in names}

    if key_names == ["month"] and period is not None:  # show empty months as 0 instead of silently omitting them
        full = pd.DataFrame({"month": _month_index(period, work["month"], coverage[1] if coverage else None)})
        res = full.merge(res, on="month", how="left")
        res[names] = res[names].fillna(0)
    for m, nm in zip(metrics, names):
        if m.op in ("count", "count_distinct") and nm in res.columns:
            res[nm] = res[nm].round().astype("int64")
    group_mean = float(res[names[0]].mean()) if sub.group_stat == "mean_of_groups" and key_names else None

    if sub.share_of_total and key_names and metrics[0].op in ("sum", "count"):
        denom = res[names[0]].sum()
        res["share_pct"] = (res[names[0]] / denom * 100).round(2) if denom else 0.0
    if key_names and not all(k in TIME_KEYS for k in key_names):
        res = res.sort_values(names[0], ascending=not sub.sort_desc, kind="stable")
    if apply_top and sub.top_n and key_names:
        res = res.head(sub.top_n)

    detail = None
    if sub.top_rows and sub.top_rows_by:
        by = resolve_column(df, sub.top_rows_by)
        base = df.assign(__v=_nums(df[by]) if _is_numeric(df[by]) else _dates(df[by])).sort_values("__v", ascending=False).head(sub.top_rows)
        detail = base.drop(columns="__v").iloc[:, :9].reset_index(drop=True)
    return ExecResult(res.reset_index(drop=True), key_names, names, len(work), rows_in_table, coverage, scalars, group_mean, detail, notes)


# ------------------------------------------------------------------ plan repair (deterministic)

_DIM_RE = re.compile(r"\b(?:which|each|per|every|by)\s+([a-z]+)(?:\s+([a-z]+))?|\b([a-z]+)[- ]wise\b", re.I)
_SKIP_DIMS = {"the", "a", "an", "month", "quarter", "year", "day", "week", "time", "period", "many", "much", "customer"} | set(TIME_KEYS)
_COUNT_Q = re.compile(r"\b(how many|number of|count of|count)\b", re.I)
_TOP_Q = re.compile(r"\b(highest|largest|biggest|most|top|lowest|least|smallest|best|worst)\b", re.I)


def _stem(col: str) -> str:
    n = _norm(col)
    return n[:-2] if n.endswith("id") and len(n) > 3 else n


def _money_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if MONEY_HINT.search(c) and _is_numeric(df[c]) and not re.search(r"rate|pct|tenure|score|level", c, re.I)]


def repair_plan(plan: AggPlan, tables: dict[str, pd.DataFrame], question: str) -> tuple[AggPlan, list[str]]:
    """Fix the mistakes small models make, using only the question text and the real schema.
    Every change is returned as a note and shown to the user ("how this was computed"). With a strong model this is a no-op."""
    if not plan.is_aggregate or plan.query is None:
        return plan, []
    q = plan.query.model_copy(deep=True)
    fixes: list[str] = []
    try:
        tname, df = resolve_table(tables, q.table)
    except PlanError:
        return plan, []
    q.table = tname
    ql = question.lower()

    # 1. date column must be a real date column; filters on derived time keys are meaningless
    try:
        if q.date_column:
            resolve_column(df, q.date_column)
    except PlanError:
        q.date_column = None
        fixes.append("date_column was not a real column; auto-detected")
    if q.date_column is None:
        q.date_column = guess_date_column(df)
    keep = [f for f in q.filters if _norm(f.column) not in {"month", "quarter", "year", "day"}]
    if len(keep) != len(q.filters):
        q.filters, _ = keep, fixes.append("dropped filters on month/quarter/year (the period is applied by the system)")

    # 2. amounts: a numeric filter whose value is prose takes the number written in the question
    nums = amounts_in(question)
    for f in q.filters:
        if f.op in (">", ">=", "<", "<=", "=", "!=") and f.column and parse_amount(f.value) is None:
            try:
                col = resolve_column(df, f.column)
            except PlanError:
                continue
            if _is_numeric(df[col]) and len(nums) == 1:
                f.value = f"{nums[0]:g}"
                fixes.append(f"filter value on {col} replaced by the amount in the question ({nums[0]:g})")

    # 2b. a derived time key (month/quarter/year) used where a real column belongs is meaningless there
    for m in q.metrics:
        if m.column and _norm(m.column) in {"month", "quarter", "year", "day"}:
            fixes.append(f"metric column '{m.column}' is not a real column; counting rows instead")
            m.op, m.column = "count", None
    if q.top_rows_by and _norm(q.top_rows_by) in {"month", "quarter", "year", "day"}:
        q.top_rows, q.top_rows_by = None, None
    if q.join and (_norm(q.join.left_on) in {"month", "quarter", "year"} or _norm(q.join.right_on) in {"month", "quarter", "year"}):
        q.join = None
        fixes.append("dropped a join on a time key")

    # 3. "how many ..." never groups by an identifier column
    if _COUNT_Q.search(question):
        ids = [g for g in q.group_by if g.lower() not in TIME_KEYS and g in df.columns and len(df) > 20 and df[g].nunique() > 0.5 * len(df)]
        if ids:
            q.group_by = [g for g in q.group_by if g not in ids]
            fixes.append(f"removed group_by on identifier column(s): {', '.join(ids)}")

    # 4. time grouping words
    for word, key in (("month-wise", "month"), ("monthly", "month"), ("each month", "month"), ("per month", "month"), ("by month", "month"),
                      ("quarter-wise", "quarter"), ("quarterly", "quarter"), ("per quarter", "quarter"), ("year-wise", "year"), ("yearly", "year")):
        if word in ql and key not in [g.lower() for g in q.group_by] and not (key == "month" and "average monthly" in ql):
            q.group_by.append(key)
            fixes.append(f"grouped by {key} because the question asks for a {word} breakdown")
            break
    if re.search(r"\btrend\b", ql) and not any(g.lower() in TIME_KEYS for g in q.group_by):
        q.group_by.append("month")
        fixes.append("grouped by month because the question asks for a trend")

    # 5. "which branch / per category / by loan type": group by that dimension, joining another table if it lives there
    cands = []
    for m in _DIM_RE.finditer(question):
        w1, w2, w3 = (m.group(1), m.group(2), m.group(3))
        if w3:
            cands.append(w3)
        if w1:
            cands += [w1 + (w2 or ""), w1]
    have_dim = any(g.lower() not in TIME_KEYS for g in q.group_by)
    for cand in dict.fromkeys(_norm(c) for c in cands):
        if have_dim or not cand or cand in _SKIP_DIMS or len(cand) < 3:
            continue
        own = [c for c in df.columns if _stem(c) in (cand, cand.rstrip("s"))]
        if own:
            q.group_by.insert(0, own[0])
            fixes.append(f"grouped by {own[0]} as the question asks for a per-{cand} answer")
            have_dim = True
            break
        for oname, odf in tables.items():
            hit = [c for c in odf.columns if _stem(c) in (cand, cand.rstrip("s"))]
            keys = [c for c in odf.columns if c in df.columns and c.lower().endswith("_id") and _stem(c) != cand]
            if oname != tname and hit and keys:
                key = next((k for k in keys if "account" in k.lower() or "customer" in k.lower()), keys[0])
                q.join = Join(table=oname, left_on=key, right_on=key)
                q.group_by.insert(0, hit[0])
                fixes.append(f"joined {oname} on {key} to group by {hit[0]}")
                have_dim = True
                break
        if have_dim:
            break

    # 6. superlatives: "which branch ... highest" = top 1; "top 3 categories" = top 3
    if have_dim and q.top_n is None and _TOP_Q.search(ql):
        n = re.search(r"\btop\s+(\d+)\b", ql)
        q.top_n = int(n.group(1)) if n else 1
        q.sort_desc = not re.search(r"\b(lowest|least|smallest|worst)\b", ql)
        fixes.append(f"kept the top {q.top_n} group(s) because the question asks for the {_TOP_Q.search(ql).group(1)}")
    if re.search(r"\btop\s+(\d+)\s+(?:transactions?|loans?|accounts?|rows?)\b|\bshow (?:the )?top (\d+)\b", ql) and not q.top_rows:
        n = re.search(r"\btop\s+(\d+)", ql)
        money = _money_columns(df)
        if n and money:
            q.top_rows, q.top_rows_by = int(n.group(1)), money[0]
            fixes.append(f"also listing the top {q.top_rows} individual rows by {money[0]}")

    # 7. money questions sum the money column; a row count of ids is almost never what "disbursed/volume/outstanding" means
    asks_money = re.search(r"disburs|volume|amount|total|recover|outstanding|principal|spent|spend|exposure", ql)
    if asks_money and not _COUNT_Q.search(question) and q.metrics and all(m.op in ("count", "count_distinct") for m in q.metrics):
        money = _money_columns(df)
        pref = next((c for c in money if re.search(r"disburs|principal", c, re.I)), None) if "disburs" in ql or "volume" in ql else None
        pref = pref or next((c for c in money if re.search("outstanding", c, re.I)), None) if "outstanding" in ql else pref
        col = pref or (money[0] if money else None)
        if col:
            q.metrics = [Metric(op="sum", column=col)]
            fixes.append(f"summed {col} instead of counting rows because the question asks for an amount")

    return plan.model_copy(update={"query": q}), fixes


# ------------------------------------------------------------------ plan -> payload


def _fmt(name: str, v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "n/a"
    if name.startswith(("count", "distinct")) or name == "count":
        return f"{int(round(v)):,}"
    s = f"{v:,.2f}"
    return f"Rs. {s}" if MONEY_HINT.search(name) else s


def _pretty(name: str) -> str:
    """'sum_amount_inr' -> 'Total amount inr' (prefix-only rewrites, so 'account' is never touched)."""
    for prefix, label in (("count_distinct_", "Distinct "), ("sum_", "Total "), ("avg_", "Average "), ("count_", "Count of "), ("min_", "Minimum "), ("max_", "Maximum ")):
        if name.startswith(prefix):
            return (label + name[len(prefix):].replace("_", " ")).strip().capitalize()
    return name.replace("_", " ").capitalize()


def _records(df: pd.DataFrame) -> list[list]:
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_float_dtype(out[c]):
            out[c] = out[c].round(2)
    return out.astype(object).where(out.notna(), None).values.tolist()


def run_plan(plan: AggPlan, tables: dict[str, pd.DataFrame], periods: list[Period], question: str = "") -> dict:
    """Execute a validated plan and return a JSON-serialisable payload: scalars (metric cards), table, chart hint,
    notes, sources and a deterministic narrative. Raises PlanError if the plan cannot be run exactly."""
    plan, repairs = repair_plan(plan, tables, question)
    sub = plan.query
    if not plan.is_aggregate or sub is None:
        raise PlanError("not an aggregate question")
    if sub.group_stat != "none" and not re.search(r"\b(average|avg|mean)\b", question, re.I):
        sub = sub.model_copy(update={"group_stat": "none"})  # the model invented an average nobody asked for
        plan = plan.model_copy(update={"query": sub})
    main = periods[0] if periods else None
    other = None
    if plan.compare_previous_period and main:
        other = periods[1] if len(periods) > 1 else previous_period(main)

    r = execute(sub, tables, main)
    payload: dict = {"period": main.label if main else None, "compare_period": other.label if other else None,
                     "rows_matched": r.rows_matched, "coverage": r.coverage, "notes": list(r.notes),
                     "sources": [sub.table] + ([sub.join.table] if sub.join else []), "scalars": [], "table": None,
                     "detail": None, "chart": None, "repairs": repairs}
    if r.rows_matched == 0:
        why = f" in {main.label}" if main else ""
        cov = f" The data covers {r.coverage[0]} to {r.coverage[1]}." if r.coverage else ""
        hint = f" ({r.empty_hint})" if r.empty_hint else ""
        payload["narrative"] = f"No rows matched your criteria{why}, so there is nothing to total.{cov}{hint}"
        return payload

    names = r.metrics
    cards = [{"label": _pretty(n) + (f" ({main.label})" if main else ""), "value": r.scalars.get(n), "display": _fmt(n, r.scalars.get(n))} for n in names if n in r.scalars]
    if r.group_mean is not None:
        cards.insert(0, {"label": f"Average of {_pretty(names[0]).lower()} per {r.keys[-1]}", "value": r.group_mean, "display": _fmt(names[0], r.group_mean)})
    cards.append({"label": "Rows matched", "value": r.rows_matched, "display": f"{r.rows_matched:,}"})

    res = r.df
    if plan.denominator:
        den = execute(plan.denominator, tables, main if plan.denominator_uses_period else None)
        dn = den.metrics[0] if den.metrics else None
        denominator = den.scalars.get(dn) if dn else None
        payload["sources"].append(plan.denominator.table)
        payload["notes"] += den.notes
        if denominator:
            pct = r.scalars[names[0]] / denominator * 100
            cards.append({"label": f"{_pretty(dn)} of {plan.denominator.table}", "value": denominator, "display": _fmt(dn, denominator)})
            cards.insert(0, {"label": "Percentage", "value": round(pct, 2), "display": f"{pct:.2f}%"})
        else:
            payload["notes"].append("the denominator is zero or empty, so no percentage was computed")

    if other is not None and r.keys is not None:
        prev = execute(sub, tables, other, only_groups=res if r.keys else None, apply_top=False)
        pn = prev.metrics[0] if prev.metrics else None
        if prev.rows_matched and pn:
            payload["notes"] += [f"[{other.label}] {n}" for n in prev.notes]
            if r.keys:
                m = res.merge(prev.df[r.keys + [pn]].rename(columns={pn: f"prev_{names[0]}"}), on=r.keys, how="left")
                m[f"prev_{names[0]}"] = m[f"prev_{names[0]}"].fillna(0)
            else:
                m = res.assign(**{f"prev_{names[0]}": prev.scalars[pn]})
            m["change"] = m[names[0]] - m[f"prev_{names[0]}"]
            m["change_pct"] = (m["change"] / m[f"prev_{names[0]}"].replace(0, pd.NA) * 100).astype("float64").round(2)
            res = m
            tot_prev = prev.scalars[pn]
            cards.append({"label": f"{_pretty(names[0])} ({other.label})", "value": tot_prev, "display": _fmt(names[0], tot_prev)})
        elif r.keys:
            m = res.copy()  # nothing in the comparison period for these groups = a genuine zero, not an error
            m[f"prev_{names[0]}"] = 0.0
            m["change"] = m[names[0]]
            m["change_pct"] = None
            res = m
            cards.append({"label": f"{_pretty(names[0])} ({other.label})", "value": 0.0, "display": _fmt(names[0], 0.0)})
            payload["notes"].append(f"these groups have no rows in {other.label}; the comparison value is 0")
        else:
            payload["notes"].append(f"no rows were found for the comparison period {other.label}")

    payload["scalars"] = cards
    if r.keys:
        if len(r.keys) == 2 and len(names) == 1 and "change" not in res.columns:
            wide = res.pivot_table(index=r.keys[0], columns=r.keys[1], values=names[0], aggfunc="sum", fill_value=0)
            wide["Total"] = wide.sum(axis=1)
            wide = wide.reset_index()
            for c in wide.columns[1:-1]:
                cards.append({"label": f"{_pretty(names[0])}: {c}", "value": float(wide[c].sum()), "display": _fmt(names[0], float(wide[c].sum()))})
            payload["table"] = {"columns": [str(c) for c in wide.columns], "rows": _records(wide)}
            if r.keys[0] in TIME_KEYS:
                payload["chart"] = {"x": str(wide.columns[0]), "y": [str(c) for c in wide.columns[1:-1]]}
        else:
            payload["table"] = {"columns": list(res.columns), "rows": _records(res)}
            if len(r.keys) == 1 and r.keys[0] in TIME_KEYS:
                payload["chart"] = {"x": r.keys[0], "y": [names[0]]}
    if r.detail is not None:
        payload["detail"] = {"title": f"Top {len(r.detail)} rows by {sub.top_rows_by}", "columns": list(r.detail.columns), "rows": _records(r.detail)}
    payload["narrative"] = narrate(payload, names, r)
    return payload


def narrate(p: dict, names: list[str], r: ExecResult) -> str:
    """Reply text built only from computed numbers (no LLM), so it cannot drift from the table."""
    period = f" for {p['period']}" if p.get("period") else ""
    lines = [f"Based on {r.rows_matched:,} matching records{period} (source: {', '.join(dict.fromkeys(p['sources']))}):"]
    for c in p["scalars"]:
        if c["label"] != "Rows matched":
            lines.append(f"- **{c['label']}:** {c['display']}")
    t = p.get("table")
    if t and t["rows"] and len(t["rows"]) <= 6:
        for row in t["rows"]:
            lines.append("- " + ", ".join(f"{c}: {v:,.2f}" if isinstance(v, float) else f"{c}: {v}" for c, v in zip(t["columns"], row)))
    elif t:
        lines.append(f"- Full breakdown ({len(t['rows'])} rows) is in the table below.")
    for n in p["notes"]:
        lines.append(f"- Note: {n}.")
    return "\n".join(lines)


# ------------------------------------------------------------------ catalog description for the planner


def _describe_column(name: str, s: pd.Series) -> str:
    vals = s[s.astype(str).str.strip() != ""]
    if vals.empty:
        return f"{name} (empty)"
    if _is_date(s):
        d = _dates(vals).dropna()
        return f"{name} (date {d.min():%Y-%m-%d}..{d.max():%Y-%m-%d})" if len(d) else f"{name} (date)"
    if _is_numeric(s):
        n = _nums(vals).dropna()
        return f"{name} (number {n.min():g}..{n.max():g})"
    uniq = list(dict.fromkeys(vals.astype(str).head(2000)))
    if len(uniq) <= 12:
        return f"{name} (values: {' | '.join(uniq)})"
    return f"{name} (text, e.g. {' | '.join(uniq[:2])})"


def describe_catalog(tables: dict[str, pd.DataFrame], question: str, k: int = 4) -> tuple[str, list[str]]:
    """Compact schema text for the k tables most relevant to the question (keeps the planner prompt small).
    Returns (text, chosen table names)."""
    words = {w for w in re.findall(r"[a-z]{3,}", question.lower())} - {"the", "and", "for", "what", "how", "many", "much", "was", "were", "last", "this", "that", "with", "from", "between", "total", "amount", "more", "than"}

    def score(item) -> float:
        name, df = item
        toks = set(re.findall(r"[a-z]{3,}", (name + " " + " ".join(df.columns)).lower().replace("_", " ")))
        stems = {t.rstrip("s") for t in toks}
        s = sum(1 for w in words if w in toks or w.rstrip("s") in stems or any(w in t or t in w for t in toks if len(t) > 4))
        if re.search(r"transaction|spend|spent|debit|credit|payment", question, re.I) and "transaction" in name.lower():
            s += 3
        if re.search(r"loan|disburs|recover|emi|npa|overdue|outstanding", question, re.I) and "loan" in name.lower():
            s += 3
        return s

    ranked = sorted(tables.items(), key=score, reverse=True)
    chosen = [n for n, _ in ranked[:k]]
    # Shared key columns between the chosen tables, so "per branch" can be answered by joining loans -> accounts.
    hints = []
    for i, a in enumerate(chosen):
        for b in chosen[i + 1:]:
            for col in set(tables[a].columns) & set(tables[b].columns):
                if col.lower().endswith("_id"):
                    hints.append(f'"{a}" and "{b}" share key {col}')
    lines = []
    for name in chosen:
        df = tables[name]
        lines.append(f'TABLE "{name}" ({len(df):,} rows)\n  ' + "\n  ".join(_describe_column(c, df[c]) for c in df.columns))
    if hints:
        lines.append("JOIN HINTS: " + "; ".join(hints[:8]))
    return "\n".join(lines), chosen
