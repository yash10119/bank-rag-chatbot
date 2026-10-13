"""Loading and classifying Excel/CSV tables. This is the ONLY ingestion path:
anything that is not a genuine .xlsx or .csv is rejected."""
import io
import re
import zipfile
from pathlib import Path

import pandas as pd

from ..config import settings
from ..security import decrypt_bytes

ALLOWED_EXT = {".csv", ".xlsx"}


class UnsupportedFile(ValueError):
    pass


def validate_upload(filename: str, content: bytes) -> str:
    """Check extension AND content signature. Returns the extension."""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise UnsupportedFile("Only .xlsx and .csv files are accepted.")
    if not content:
        raise UnsupportedFile("The file is empty.")
    if ext == ".xlsx":
        if not content.startswith(b"PK"):
            raise UnsupportedFile("File content is not a valid .xlsx workbook.")
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                if "xl/workbook.xml" not in z.namelist():
                    raise UnsupportedFile("File content is not a valid .xlsx workbook.")
        except zipfile.BadZipFile as exc:
            raise UnsupportedFile("File content is not a valid .xlsx workbook.") from exc
    else:
        if content.startswith(b"PK") or b"\x00" in content[:4096]:
            raise UnsupportedFile("File content is binary, not CSV text.")
    return ext


def _clean_headers(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(how="all").dropna(axis=1, how="all")
    cols, seen = [], {}
    for i, c in enumerate(df.columns):
        name = re.sub(r"\s+", " ", str(c)).strip()
        if not name or name.lower().startswith("unnamed"):
            name = f"col_{i + 1}"
        seen[name] = seen.get(name, 0) + 1
        cols.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    df.columns = cols
    df = df.fillna("")
    return df.reset_index(drop=True)


def load_tables(path: str | Path, ext: str) -> list[tuple[str, pd.DataFrame]]:
    """Blocking (pandas); call via asyncio.to_thread. Returns [(sheet_name, df)] with all cells as str."""
    limit = settings.max_table_rows
    if ext == ".csv":
        raw = decrypt_bytes(Path(path).read_bytes())
        for enc in ("utf-8-sig", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        df = pd.read_csv(io.StringIO(text), sep=None, engine="python", dtype=str, nrows=limit + 1)
        sheets = {"csv": df}
    elif ext == ".xlsx":
        sheets = pd.read_excel(io.BytesIO(decrypt_bytes(Path(path).read_bytes())), sheet_name=None, dtype=str, engine="openpyxl", nrows=limit + 1)
    else:
        raise UnsupportedFile("Only .xlsx and .csv files are accepted.")
    out = []
    for name, df in sheets.items():
        df = _clean_headers(df)
        if df.empty:
            continue
        if len(df) > limit:
            raise UnsupportedFile(f"Sheet '{name}' exceeds the {limit:,}-row limit.")
        out.append((str(name), df))
    if not out:
        raise UnsupportedFile("No data rows found in the file.")
    return out


def _norm(col: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", col.lower()).strip("_")


def find_col(columns: list[str], *needles: str) -> str | None:
    normed = {c: _norm(c) for c in columns}
    for needle in needles:  # priority by needle order
        for c, n in normed.items():
            if needle in n:
                return c
    return None


def classify_table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    joined = " ".join(_norm(c) for c in cols)
    has = lambda *k: any(x in joined for x in k)  # noqa: E731
    if has("date", "timestamp") and has("amount", "debit", "credit") and has("description", "narration", "type", "merchant", "balance"):
        return "transactions"
    if has("customer", "email") and has("credit_score", "income", "relationship", "rm_"):
        return "profile"
    if has("eligib"):
        return "eligibility_rules"
    if has("rate") and has("tenure", "term", "loan"):
        return "rates"
    if has("threshold", "reporting_limit"):
        return "compliance"
    if has("fee", "charge"):
        return "fees"
    return "other"


def to_number(value: object) -> float | None:
    s = str(value).strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[^\d.\-]", "", s)
    if s in ("", "-", ".", "-."):
        return None
    try:
        n = float(s)
    except ValueError:
        return None
    return -abs(n) if neg else n
