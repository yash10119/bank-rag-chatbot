"""Exact, deterministic reads over a user's own uploaded files (still Excel/CSV only).
Retrieval finds relevant rows; this module answers 'what is the profile / what looks
unusual' from the complete tables so numbers are never approximated by an LLM."""
import asyncio
import re
import statistics
import uuid
from pathlib import Path

import pandas as pd
from sqlalchemy import select

from ..db import SessionLocal
from ..models import Document
from .tables import classify_table, find_col, load_tables, to_number


def _key(col: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", col.lower()).strip("_")


async def _user_tables(user_id: str, kind: str | None, include_bank: bool = False) -> list[tuple[Document, str, pd.DataFrame]]:
    """Documents OWNED by this user (scope=customer), plus bank-wide ones when include_bank.
    kind=None returns every table."""
    mine = (Document.owner_id == uuid.UUID(user_id)) & (Document.scope == "customer")
    async with SessionLocal() as s:
        docs = (
            await s.execute(
                select(Document)
                .where((mine | (Document.scope == "bank")) if include_bank else mine, Document.status == "ready")
                .order_by(Document.created_at.desc())
            )
        ).scalars().all()
    out = []
    for d in docs:
        try:
            tables = await asyncio.to_thread(load_tables, Path(d.storage_path), d.ext)
        except Exception:
            continue
        out += [(d, sheet, df) for sheet, df in tables if kind is None or classify_table(df) == kind]
    return out


_STOP = {"the", "a", "an", "of", "in", "on", "for", "to", "and", "or", "is", "was", "has", "have", "how", "what", "over", "last",
         "days", "day", "week", "weeks", "month", "months", "change", "changed", "rate", "rates", "current", "now", "trend", "year"}


def _window_days(question: str) -> int | None:
    q = question.lower()
    if m := re.search(r"(\d+)\s*(day|week|month)", q):
        return int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2)]
    for word, days in (("week", 7), ("month", 30), ("fortnight", 14)):
        if word in q:
            return days
    return None


def series_trends(df: pd.DataFrame, question: str) -> list[dict]:
    """Exact first/last/min/max/change per category for a long-format time series
    (date, <category>, <numeric value>) such as daily product rates or FX rates."""
    cols = list(df.columns)
    date_c = find_col(cols, "date")
    cat_c = find_col(cols, "product", "currency", "pair", "instrument", "name")
    val_cs = [c for c in cols if re.search(r"rate|price|yield|value", _key(c)) and c != cat_c]
    if not (date_c and cat_c and val_cs):
        return []
    val_c = next((c for c in val_cs if "mid" in _key(c)), val_cs[0])
    t = pd.DataFrame({"date": pd.to_datetime(df[date_c], errors="coerce"), "cat": df[cat_c], "v": df[val_c].map(to_number)}).dropna()
    if t.empty:
        return []
    days = _window_days(question)
    if days:
        t = t[t["date"] >= t["date"].max() - pd.Timedelta(days=days)]
    no_window = re.sub(r"\d+\s*(day|week|month)s?", " ", question.lower())  # "30 days" is not a product number
    words = {w for w in re.findall(r"[a-z0-9]+", no_window) if w not in _STOP and (len(w) > 1 or w.isdigit())}
    cats = list(t["cat"].unique())
    nums = {w for w in words if w.isdigit()}

    def score(c) -> int:
        toks = set(re.findall(r"[a-z0-9]+", str(c).lower()))
        s = len(words & toks)
        if nums and (toks & {x for x in toks if x.isdigit()}) and not nums & toks:
            s -= 2  # "1-year FD" must not match "FD 3 year"
        return s

    scores = {c: score(c) for c in cats}
    best = max(scores.values())
    matched = [c for c in cats if scores[c] == best] if best > 0 else []
    out = []
    for cat in matched or cats:
        g = t[t["cat"] == cat].sort_values("date")
        first, last = g.iloc[0], g.iloc[-1]
        out.append({
            "series": str(cat), "metric": val_c, "from": f"{first['date']:%Y-%m-%d}", "to": f"{last['date']:%Y-%m-%d}",
            "start_value": round(float(first.v), 4), "end_value": round(float(last.v), 4),
            "change": round(float(last.v - first.v), 4), "min": round(float(g.v.min()), 4), "max": round(float(g.v.max()), 4),
            "observations": len(g),
        })
    return out


async def analyze_series(user_id: str, question: str) -> list[dict]:
    """Trends from every date-indexed rate table the caller may read (own + bank-wide files)."""
    results = []
    for doc, sheet, df in await _user_tables(user_id, None, include_bank=True):
        if find_col(list(df.columns), "amount"):  # transactions are handled elsewhere
            continue
        for r in await asyncio.to_thread(series_trends, df, question):
            results.append({**r, "source": f"{doc.filename} > {sheet}"})
    return results[:12]


async def load_profile(user_id: str) -> dict | None:
    """Most recent customer profile row, keys normalised to snake_case."""
    for _doc, _sheet, df in await _user_tables(user_id, "profile"):
        row = df.iloc[0].to_dict()
        profile = {_key(k): v for k, v in row.items() if str(v).strip()}
        for numeric in ("credit_score", "annual_income", "monthly_debt", "existing_emi", "account_balance"):
            if numeric in profile:
                profile[numeric] = to_number(profile[numeric]) if numeric != "credit_score" else int(to_number(profile[numeric]) or 0)
        return profile
    return None


def analyze_frame(df: pd.DataFrame, large_threshold: float) -> dict:
    cols = list(df.columns)
    date_c = find_col(cols, "date", "timestamp")
    amount_c, debit_c, credit_c = find_col(cols, "amount"), find_col(cols, "debit", "withdraw"), find_col(cols, "credit_amount", "deposit")
    type_c = find_col(cols, "type", "dr_cr", "direction")
    desc_c = find_col(cols, "description", "narration", "merchant", "details", "memo")
    id_c = find_col(cols, "txn_id", "transaction_id", "reference", "id")

    if debit_c or credit_c:
        signed = (df[credit_c].map(to_number).fillna(0) if credit_c else 0) - (df[debit_c].map(to_number).fillna(0) if debit_c else 0)
    elif amount_c:
        raw = df[amount_c].map(to_number)
        if type_c:
            is_debit = df[type_c].str.lower().str.contains(r"debit|dr\b|withdraw|out|transfer|purchase|payment", regex=True)
            signed = raw.abs().where(~is_debit, -raw.abs())
        else:
            signed = raw
    else:
        return {"rows": len(df), "error": "no amount column recognised"}
    t = pd.DataFrame({"amt": signed})
    t["date"] = pd.to_datetime(df[date_c], errors="coerce", utc=True) if date_c else pd.NaT
    t["desc"] = df[desc_c] if desc_c else ""
    t["id"] = df[id_c] if id_c else df.index.astype(str)
    t = t.dropna(subset=["amt"])

    debits = t[t.amt < 0]
    credits_ = t[t.amt > 0]
    flags: list[dict] = []

    for _, r in debits[debits.amt.abs() >= large_threshold].iterrows():
        flags.append({"type": "large_transaction", "severity": "high", "txn": r["id"], "amount": abs(r.amt),
                      "detail": f"{abs(r.amt):,.2f} debit ({r['desc']}) meets/exceeds reporting threshold {large_threshold:,.0f}"})
    amounts = [abs(a) for a in debits.amt]
    if len(amounts) >= 8:
        mean, sd = statistics.mean(amounts), statistics.pstdev(amounts)
        if sd > 0:
            for _, r in debits.iterrows():
                if abs(r.amt) >= large_threshold:
                    continue
                if (abs(r.amt) - mean) / sd > 3:
                    flags.append({"type": "statistical_outlier", "severity": "medium", "txn": r["id"], "amount": abs(r.amt),
                                  "detail": f"{abs(r.amt):,.2f} is >3 std-dev above the account's usual debit ({mean:,.2f})"})
    if t["date"].notna().any() and len(debits) >= 5:
        d = debits.dropna(subset=["date"]).sort_values("date")
        times = list(d["date"])
        for i in range(len(times) - 4):
            if (times[i + 4] - times[i]).total_seconds() <= 3600:
                flags.append({"type": "rapid_succession", "severity": "high", "txn": d.iloc[i]["id"], "amount": None,
                              "detail": "5+ debits within one hour"})
                break
        dup = d.groupby([d["date"].dt.date, d.amt.round(2)]).size()
        for (day, amt), n in dup.items():
            if n >= 3:
                flags.append({"type": "repeated_amount", "severity": "medium", "txn": None, "amount": abs(amt),
                              "detail": f"{n} identical debits of {abs(amt):,.2f} on {day}"})
                break

    period = None
    if t["date"].notna().any():
        period = f"{t['date'].min():%Y-%m-%d} to {t['date'].max():%Y-%m-%d}"
    recent = t.dropna(subset=["date"]).sort_values("date", ascending=False).head(5)
    return {
        "rows": len(t),
        "period": period,
        "total_debits": round(float(debits.amt.abs().sum()), 2),
        "total_credits": round(float(credits_.amt.sum()), 2),
        "largest_debits": [{"txn": r["id"], "amount": round(abs(r.amt), 2), "desc": r["desc"]}
                           for _, r in debits.reindex(debits.amt.abs().sort_values(ascending=False).index).head(3).iterrows()],
        "recent": [{"date": f"{r['date']:%Y-%m-%d}", "amount": round(r.amt, 2), "desc": r["desc"]} for _, r in recent.iterrows()],
        "flags": flags[:10],
    }


async def analyze_transactions(user_id: str, large_threshold: float) -> dict | None:
    tables = await _user_tables(user_id, "transactions")
    if not tables:
        return None
    doc, sheet, df = tables[0]
    result = await asyncio.to_thread(analyze_frame, df, large_threshold)
    result["source"] = f"{doc.filename} > {sheet}"
    return result


async def load_catalog(user_id: str) -> dict[str, pd.DataFrame]:
    """Every table the caller may aggregate over (own uploads + bank-wide files), keyed by a stable name.
    Excel workbooks whose sheets share the same columns also get a combined `<file>::*` table (adds a `sheet` column),
    so "all branches" questions work when each sheet is a branch/product."""
    out: dict[str, pd.DataFrame] = {}
    sheets_by_file: dict[str, list[tuple[str, pd.DataFrame]]] = {}
    for doc, sheet, df in await _user_tables(user_id, None, include_bank=True):  # newest upload first
        name = doc.filename if sheet == "csv" else f"{doc.filename}::{sheet}"
        if name in out:
            continue  # re-uploaded file: the newest copy wins
        out[name] = df
        sheets_by_file.setdefault(doc.filename, []).append((sheet, df))
    for fname, sheets in sheets_by_file.items():
        if len(sheets) > 1 and all(list(d.columns) == list(sheets[0][1].columns) for _, d in sheets):
            out[f"{fname}::*"] = pd.concat([d.assign(sheet=s) for s, d in sheets], ignore_index=True)
    return out
