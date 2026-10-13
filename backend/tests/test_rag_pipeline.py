import io

import pandas as pd
import pytest

from app import demo_data
from app.rag.analytics import analyze_frame
from app.rag.chunking import build_chunks
from app.rag.store import scope_filter
from app.rag.tables import UnsupportedFile, classify_table, load_tables, to_number, validate_upload


def test_rejects_non_excel_csv_files():
    for name, content in [("a.pdf", b"%PDF-1.4"), ("a.txt", b"hello"), ("a.json", b"{}"), ("a.xlsm", b"PK")]:
        with pytest.raises(UnsupportedFile):
            validate_upload(name, content)


def test_rejects_spoofed_extensions():
    with pytest.raises(UnsupportedFile):
        validate_upload("evil.xlsx", b"not a zip at all")
    with pytest.raises(UnsupportedFile):
        validate_upload("evil.csv", b"PK\x03\x04binary")
    with pytest.raises(UnsupportedFile):
        validate_upload("empty.csv", b"")


def test_accepts_generated_files(tmp_path):
    for name, content in demo_data.bank_files().items():
        ext = validate_upload(name, content)
        p = tmp_path / name
        p.write_bytes(content)
        assert load_tables(p, ext)


def test_table_kinds_detected(tmp_path):
    kinds = {}
    for name, content in {**demo_data.bank_files(), **demo_data.customer_files("A B", "a@b.com")}.items():
        p = tmp_path / name
        p.write_bytes(content)
        kinds[name] = {classify_table(df) for _, df in load_tables(p, p.suffix)}
    assert kinds["loan_interest_rates.csv"] == {"rates"}
    assert kinds["loan_eligibility_rules.csv"] == {"eligibility_rules"}
    assert kinds["product_fee_schedule.xlsx"] == {"fees"}
    assert kinds["compliance_thresholds.csv"] == {"compliance"}
    assert kinds["customer_profile.xlsx"] == {"profile"}
    assert kinds["transaction_history.csv"] == {"transactions"}


def test_chunking_row_schema_and_summary(tmp_path):
    p = tmp_path / "t.csv"
    p.write_bytes(demo_data.customer_files("A B", "a@b.com")["transaction_history.csv"])
    ((sheet, df),) = load_tables(p, ".csv")
    chunks = build_chunks("t.csv", sheet, "transactions", df)
    kinds = [c.kind for c in chunks]
    assert kinds.count("schema") == 1
    assert kinds.count("row") == len(df)
    assert kinds.count("summary") >= 1
    row = next(c for c in chunks if c.kind == "row")
    assert "t.csv" in row.text and "description:" in row.text and row.data


def test_rate_row_text_is_searchable(tmp_path):
    p = tmp_path / "r.csv"
    p.write_bytes(demo_data.bank_files()["loan_interest_rates.csv"])
    ((sheet, df),) = load_tables(p, ".csv")
    texts = [c.text for c in build_chunks("r.csv", sheet, "rates", df) if c.kind == "row"]
    assert any("product: Home Loan" in t and "tenure_years: 5" in t and "min_credit_score: 750" in t for t in texts)


def test_to_number():
    assert to_number("$1,234.50") == 1234.5
    assert to_number("(200)") == -200
    assert to_number("abc") is None and to_number("") is None


def test_analytics_flags_large_transfer(tmp_path):
    p = tmp_path / "t.csv"
    p.write_bytes(demo_data.customer_files("A B", "a@b.com")["transaction_history.csv"])
    ((_, df),) = load_tables(p, ".csv")
    res = analyze_frame(df, large_threshold=10000)
    assert any(f["type"] == "large_transaction" and f["amount"] == 48500 for f in res["flags"])
    assert res["total_debits"] > 48500
    assert res["rows"] == len(df)
    quiet = analyze_frame(df, large_threshold=10**9)
    assert not [f for f in quiet["flags"] if f["type"] == "large_transaction"]


def test_scope_filter_always_pins_owner():
    f = scope_filter("user-A", "customer", None)
    dumped = f.model_dump_json()
    assert "user-A" in dumped and "customer" in dumped
    assert "bank" not in dumped  # customer scope never widens to other tenants
    both = scope_filter("user-A", "all", ["rates"]).model_dump_json()
    assert "user-A" in both and "bank" in both


def test_xlsx_roundtrip_with_multiple_sheets(tmp_path):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        pd.DataFrame({"a": [1, 2]}).to_excel(w, sheet_name="S1", index=False)
        pd.DataFrame({"b": ["x"]}).to_excel(w, sheet_name="S2", index=False)
    p = tmp_path / "m.xlsx"
    p.write_bytes(buf.getvalue())
    assert [s for s, _ in load_tables(p, ".xlsx")] == ["S1", "S2"]
