"""Table-aware chunking.

Three chunk kinds per sheet:
  schema  - columns, row count, numeric ranges, categorical values (answers "what does this file contain")
  row     - one row (or a small group for very large tables) rendered as `column: value; ...`
  summary - monthly aggregates for transaction-like tables (answers period/total questions)
Every chunk carries its file/sheet in the text so it is self-describing at retrieval time.
"""
import math
from dataclasses import dataclass, field

import pandas as pd

from .tables import find_col, to_number

MAX_ROW_CHUNKS = 5000
MAX_GROUP = 40


@dataclass
class Chunk:
    """One indexable unit: its text, kind (schema, row or summary), sheet and source row range."""
    text: str
    kind: str
    sheet: str
    row_start: int = 0
    row_end: int = 0
    data: dict = field(default_factory=dict)


def _schema_chunk(filename: str, sheet: str, table_kind: str, df: pd.DataFrame) -> Chunk:
    """One chunk describing the sheet itself: columns, row count, numeric ranges and small value sets."""
    parts = [f"[{filename} > {sheet}] SCHEMA. Table type: {table_kind}. {len(df)} rows. Columns: {', '.join(df.columns)}."]
    for c in df.columns:
        nums = [n for n in (to_number(v) for v in df[c]) if n is not None]
        if len(nums) >= 0.8 * len(df) and nums:
            parts.append(f"{c}: numeric {min(nums):g} to {max(nums):g}.")
        else:
            uniq = [u for u in df[c].unique() if str(u).strip()]
            if 0 < len(uniq) <= 15:
                parts.append(f"{c}: values {', '.join(map(str, uniq))}.")
    return Chunk(" ".join(parts)[:3000], "schema", sheet, 0, len(df))


def _row_text(filename: str, sheet: str, row: pd.Series) -> str:
    """Render a row as 'column: value; ...' prefixed with file and sheet, so each chunk is self-describing."""
    body = "; ".join(f"{c}: {v}" for c, v in row.items() if str(v).strip())
    return f"[{filename} > {sheet}] {body}"


def _summaries(filename: str, sheet: str, df: pd.DataFrame) -> list[Chunk]:
    """Monthly inflow / outflow / net chunks for transaction tables, for quick period questions."""
    cols = list(df.columns)
    date_c = find_col(cols, "date", "timestamp")
    debit_c, credit_c = find_col(cols, "debit", "withdraw"), find_col(cols, "credit_amount", "deposit")
    amount_c = find_col(cols, "amount")
    if not date_c or not (amount_c or debit_c):
        return []
    dates = pd.to_datetime(df[date_c], errors="coerce", utc=True)
    if dates.notna().sum() == 0:
        return []
    amt = df[amount_c].map(to_number) if amount_c else pd.Series([None] * len(df))
    tmp = pd.DataFrame({"m": dates.dt.strftime("%Y-%m"), "amt": amt})
    if debit_c:
        tmp["amt"] = df[credit_c].map(to_number).fillna(0) - df[debit_c].map(to_number).fillna(0) if credit_c else -df[debit_c].map(to_number).fillna(0)
    out = []
    for month, g in tmp.dropna(subset=["m", "amt"]).groupby("m"):
        inflow, outflow = g.loc[g.amt > 0, "amt"].sum(), -g.loc[g.amt < 0, "amt"].sum()
        out.append(
            Chunk(
                f"[{filename} > {sheet}] MONTHLY SUMMARY {month}: {len(g)} transactions, "
                f"total inflow {inflow:,.2f}, total outflow {outflow:,.2f}, net {inflow - outflow:,.2f}.",
                "summary",
                sheet,
            )
        )
    return out


def build_chunks(filename: str, sheet: str, table_kind: str, df: pd.DataFrame) -> list[Chunk]:
    """All chunks for one sheet. Rows are indexed one per chunk; very large sheets group rows so a sheet
    never exceeds MAX_ROW_CHUNKS. Row ranges are kept so answers can cite file, sheet and rows.
    """
    chunks = [_schema_chunk(filename, sheet, table_kind, df)]
    group = min(MAX_GROUP, max(1, math.ceil(len(df) / MAX_ROW_CHUNKS)))
    for start in range(0, len(df), group):
        block = df.iloc[start : start + group]
        text = "\n".join(_row_text(filename, sheet, r) for _, r in block.iterrows())
        data = block.iloc[0].to_dict() if group == 1 else {}
        chunks.append(Chunk(text[:4000], "row", sheet, start, start + len(block) - 1, data))
    if table_kind == "transactions":
        chunks += _summaries(filename, sheet, df)
    return chunks
