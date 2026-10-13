"""Synthetic bank + customer files (generated, not committed as binaries)."""
import hashlib
import io
from datetime import datetime, timedelta, timezone

import pandas as pd

from .config import settings


def _csv(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode()


def _xlsx(sheets: dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for name, df in sheets.items():
            df.to_excel(w, sheet_name=name, index=False)
    return buf.getvalue()


def bank_files() -> dict[str, bytes]:
    """Shared reference files seeded on first boot: interest rates, eligibility rules, fee schedule (two sheets)
    and compliance thresholds. Returned as {filename: bytes} and ingested like any upload.
    """
    rates = []
    bands = [(300, 649, 3.0), (650, 699, 1.5), (700, 749, 0.75), (750, 799, 0.0), (800, 900, -0.25)]
    base = {("Home Loan", 5): 8.75, ("Home Loan", 10): 8.95, ("Home Loan", 20): 9.10,
            ("Personal Loan", 3): 11.25, ("Personal Loan", 5): 11.75, ("Auto Loan", 5): 9.40}
    for (product, years), b in base.items():
        for lo, hi, adj in bands:
            rates.append({"product": product, "tenure_years": years, "min_credit_score": lo, "max_credit_score": hi,
                          "interest_rate_pct": round(b + adj - 0.40, 2) if lo >= 750 else round(b + adj, 2),
                          "processing_fee_pct": 0.5 if product == "Home Loan" else 1.5})
    eligibility = [
        {"product": "Home Loan", "eligibility_rule": "Minimum credit score", "value": "650"},
        {"product": "Home Loan", "eligibility_rule": "Maximum EMI to monthly income ratio (%)", "value": "50"},
        {"product": "Home Loan", "eligibility_rule": "Minimum annual income", "value": "400000"},
        {"product": "Home Loan", "eligibility_rule": "Maximum loan amount as multiple of annual income", "value": "5"},
        {"product": "Personal Loan", "eligibility_rule": "Minimum credit score", "value": "700"},
        {"product": "Personal Loan", "eligibility_rule": "Minimum annual income", "value": "300000"},
    ]
    fees = {
        "Account Fees": pd.DataFrame([
            {"item": "Savings account monthly maintenance", "fee": "0", "condition": "Average balance above 10,000"},
            {"item": "Savings account monthly maintenance", "fee": "150", "condition": "Average balance below 10,000"},
            {"item": "Duplicate statement (paper)", "fee": "100", "condition": "Per statement"},
            {"item": "Cheque book (25 leaves)", "fee": "0", "condition": "First per year"},
        ]),
        "Transfer Fees": pd.DataFrame([
            {"item": "NEFT", "fee": "0", "condition": "Any amount"},
            {"item": "RTGS", "fee": "25", "condition": "Above 200,000"},
            {"item": "International wire", "fee": "1500", "condition": "Per transfer"},
        ]),
    }
    compliance = [
        {"control": "Large cash or transfer reporting threshold", "threshold_amount": "10000", "action": "Flag for officer review"},
        {"control": "Structuring detection (5+ debits in 1 hour)", "threshold_amount": "5", "action": "Alert compliance"},
        {"control": "International wire enhanced due diligence", "threshold_amount": "5000", "action": "Manual approval"},
    ]
    return {
        "loan_interest_rates.csv": _csv(pd.DataFrame(rates)),
        "loan_eligibility_rules.csv": _csv(pd.DataFrame(eligibility)),
        "product_fee_schedule.xlsx": _xlsx(fees),
        "compliance_thresholds.csv": _csv(pd.DataFrame(compliance)),
    }


def customer_files(name: str, email: str) -> dict[str, bytes]:
    """Sample profile workbook and transaction history for one user (the Load demo data button)."""
    cust_id = "CUST-" + hashlib.sha1(email.encode()).hexdigest()[:6].upper()
    profile = pd.DataFrame([{
        "customer_id": cust_id, "customer_name": name, "email": email, "credit_score": 782,
        "annual_income": 1800000, "monthly_debt_obligations": 15000, "employment_type": "Salaried",
        "account_balance": 245000, "relationship_manager_name": settings.demo_rm_name,
        "rm_email": settings.demo_rm_email, "kyc_status": "Verified",
    }])
    now = datetime.now(timezone.utc).replace(microsecond=0)
    rows, bal = [], 245000.0
    spend = [(-1200, "Grocery"), (-450, "Fuel"), (-3200, "Electricity bill"), (-899, "Streaming"), (-2500, "Restaurant")]
    for i in range(30):
        amt, desc = spend[i % len(spend)]
        bal += amt
        rows.append({"txn_id": f"T{1000 + i}", "date": (now - timedelta(days=30 - i)).strftime("%Y-%m-%d %H:%M:%S"),
                     "description": desc, "type": "debit", "amount": abs(amt), "balance": round(bal, 2)})
    rows.append({"txn_id": "T2001", "date": (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
                 "description": "Transfer to unknown beneficiary", "type": "debit", "amount": 48500, "balance": round(bal - 48500, 2)})
    rows.append({"txn_id": "T2002", "date": (now - timedelta(days=12)).strftime("%Y-%m-%d %H:%M:%S"),
                 "description": "Salary", "type": "credit", "amount": 150000, "balance": round(bal + 150000, 2)})
    return {
        "customer_profile.xlsx": _xlsx({"Profile": profile}),
        "transaction_history.csv": _csv(pd.DataFrame(rows)),
    }
