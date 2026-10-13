"""The aggregation engine must give exact answers for the assessment's example query classes."""
from datetime import date

import pandas as pd
import pytest

from app.rag.aggregate import AggPlan, Filter, Join, Metric, PlanError, SubQuery, execute, run_plan
from app.rag.periods import parse_periods

TODAY = date(2026, 10, 12)


def tx():
    rows = [  # timestamp, type, amount, category, status
        ("2026-01-10 10:00:00", "Debit", "1000", "Groceries"), ("2026-01-20 10:00:00", "Credit", "5000", "Salary"),
        ("2026-02-05 10:00:00", "Debit", "20000", "Shopping"), ("2026-02-06 10:00:00", "Debit", "500", "Dining"),
        ("2026-04-02 10:00:00", "Debit", "12000", "Shopping"), ("2026-04-20 10:00:00", "Credit", "30000", "Salary"),
        ("2026-05-03 10:00:00", "Debit", "800", "Groceries"), ("2026-06-30 23:59:00", "Debit", "10000", "Dining"),
        ("2026-07-01 00:00:00", "Debit", "99999", "Shopping"), ("2026-08-15 10:00:00", "Debit", "15000", "Shopping"),
        ("2026-09-10 10:00:00", "Debit", "300", "Groceries"), ("2026-09-11 10:00:00", "Credit", "700", "Salary"),
    ]
    return pd.DataFrame(rows, columns=["timestamp", "type", "amount_inr", "category"]).assign(account_id=[f"A{i % 3}" for i in range(12)])


T = {"tx.csv": tx()}


def plan(**q):
    q.setdefault("table", "tx.csv")
    return AggPlan(is_aggregate=True, query=SubQuery(**q))


def periods(text):
    return parse_periods(text, TODAY)


def cards(p):
    return {c["label"]: c["value"] for c in p["scalars"]}


def test_total_debit_credit_month_wise_apr_to_jun():
    p = run_plan(plan(date_column="timestamp", group_by=["month", "type"], metrics=[Metric(op="sum", column="amount_inr")]),
                 T, periods("total between April and June this year"))
    cols, rows = p["table"]["columns"], {r[0]: r for r in p["table"]["rows"]}
    assert cols == ["month", "Credit", "Debit", "Total"]
    assert rows["2026-04"][1:3] == [30000.0, 12000.0] and rows["2026-05"][2] == 800.0 and rows["2026-06"][2] == 10000.0
    assert cards(p)["Total amount inr: Credit"] == 30000.0 and cards(p)["Total amount inr: Debit"] == 22800.0
    assert "Rs." in p["narrative"] and p["chart"]["x"] == "month"


def test_count_over_threshold_last_quarter_with_top_rows():
    # last quarter relative to 2026-10-12 = Q3 (Jul-Sep): debits > 10000 are 99999 (Jul 1) and 15000
    p = run_plan(plan(date_column="timestamp", filters=[Filter(column="type", op="=", value="debit"), Filter(column="amount_inr", op=">", value="10000")],
                      metrics=[Metric(op="count")], top_rows=5, top_rows_by="amount_inr"), T, periods("how many times over 10,000 last quarter"))
    assert cards(p)["Count (Q3 2026)"] == 2 and p["detail"]["rows"][0][2] == "99999"


def test_average_monthly_spend_is_mean_of_group_totals_including_empty_months():
    p = run_plan(plan(date_column="timestamp", filters=[Filter(column="type", op="=", value="Debit"), Filter(column="category", op="in", value="Shopping, Groceries, Dining")],
                      group_by=["month"], metrics=[Metric(op="sum", column="amount_inr")], group_stat="mean_of_groups"),
                 T, periods("average monthly spending in the first half of this year"), "What was my average monthly spending in the first half of this year?")
    months = {r[0]: r[1] for r in p["table"]["rows"]}
    assert months["2026-03"] == 0 and len(months) == 6  # March had no spend but still counts as a month
    assert cards(p)["Average of total amount inr per month"] == pytest.approx((1000 + 20000 + 500 + 12000 + 800 + 10000) / 6)


def test_top_categories_with_share_of_total():
    p = run_plan(plan(date_column="timestamp", filters=[Filter(column="type", op="=", value="Debit")], group_by=["category"],
                      metrics=[Metric(op="sum", column="amount_inr")], top_n=2, share_of_total=True), T, periods("last 3 months"))
    rows = p["table"]["rows"]
    assert [r[0] for r in rows] == ["Shopping", "Groceries"] and rows[0][2] == pytest.approx(15000 / 15300 * 100, abs=0.01)


def test_quarter_over_quarter_for_top_group_with_join():
    loans = pd.DataFrame({"loan_id": list("abcd"), "account_id": ["A0", "A1", "A0", "A2"], "start_date": ["2026-02-01", "2026-03-01", "2025-11-01", "2025-12-01"],
                          "principal_inr": ["100", "50", "30", "80"]})
    accounts = pd.DataFrame({"account_id": ["A0", "A1", "A2"], "branch_id": ["BR1", "BR2", "BR2"]})
    p = run_plan(AggPlan(is_aggregate=True, compare_previous_period=True, query=SubQuery(
        table="loans", date_column="start_date", group_by=["branch_id"], metrics=[Metric(op="sum", column="principal_inr")], top_n=1,
        join=Join(table="accounts", left_on="account_id", right_on="account_id"))),
        {"loans": loans, "accounts": accounts}, periods("Which branch disbursed the most in Q1 2026 and compare to Q4 2025"))
    cols, row = p["table"]["columns"], p["table"]["rows"][0]
    got = dict(zip(cols, row))
    assert got["branch_id"] == "BR1" and got["sum_principal_inr"] == 100 and got["prev_sum_principal_inr"] == 30 and got["change"] == 70


def test_percentage_across_two_tables():
    rec = pd.DataFrame({"date": ["2026-01-05", "2026-02-05", "2026-05-05"], "recovered": ["200", "300", "999"]})
    port = pd.DataFrame({"outstanding": ["1000", "1000"]})
    p = run_plan(AggPlan(is_aggregate=True, query=SubQuery(table="rec", date_column="date", metrics=[Metric(op="sum", column="recovered")]),
                         denominator=SubQuery(table="port", metrics=[Metric(op="sum", column="outstanding")])), {"rec": rec, "port": port}, periods("recovered in Q1 2026"))
    assert cards(p)["Percentage"] == 25.0


def test_count_distinct_with_two_filters_npa_style():
    loans = pd.DataFrame({"account_id": ["a", "a", "b", "c"], "date": ["2026-10-02"] * 3 + ["2026-04-01"], "days_overdue": ["95", "120", "30", "200"], "out": ["10", "10", "5", "7"]})
    p = run_plan(plan(table="l", date_column="date", filters=[Filter(column="days_overdue", op=">", value="90")],
                      metrics=[Metric(op="count_distinct", column="account_id"), Metric(op="sum", column="out")]), {"l": loans}, periods("this quarter"))
    assert cards(p)["Distinct account id (Q4 2026)"] == 1


def test_empty_match_reports_instead_of_zero_and_lists_real_values():
    p = run_plan(plan(date_column="timestamp", filters=[Filter(column="category", op="=", value="retail")], metrics=[Metric(op="sum", column="amount_inr")]), T, [])
    assert p["rows_matched"] == 0 and "No rows matched" in p["narrative"] and "Groceries" in p["narrative"]


def test_period_beyond_data_is_flagged():
    p = run_plan(plan(date_column="timestamp", metrics=[Metric(op="count")]), T, periods("in 2026"))
    assert any("extends beyond the data" in n for n in p["notes"])


def test_unknown_column_or_table_is_an_error_not_a_guess():
    with pytest.raises(PlanError):
        run_plan(plan(metrics=[Metric(op="sum", column="no_such")]), T, [])
    with pytest.raises(PlanError):
        run_plan(plan(table="nope.csv", metrics=[Metric(op="count")]), T, [])
    with pytest.raises(PlanError):
        run_plan(plan(date_column="timestamp", metrics=[Metric(op="sum", column="category")]), T, [])


def test_fuzzy_column_names_resolve():
    r = execute(SubQuery(table="tx.csv", metrics=[Metric(op="sum", column="Amount INR")]), T, None)
    assert r.scalars["sum_Amount INR"] == sum(float(x) for x in tx().amount_inr)


def test_models_own_date_filters_are_ignored_when_the_system_resolved_the_period():
    # The LLM re-added the period as a (wrong) date filter; the code-resolved period must win.
    p = run_plan(plan(date_column="timestamp", filters=[Filter(column="timestamp", op="in", value="2026-04-01..2026-06-30")],
                      metrics=[Metric(op="count")]), T, periods("between April and June this year"))
    assert p["rows_matched"] == 4 and any("was ignored" in n for n in p["notes"])


def test_date_only_upper_bound_is_day_inclusive_without_a_resolved_period():
    p = run_plan(plan(date_column="timestamp", filters=[Filter(column="timestamp", op="<=", value="2026-06-30")],
                      metrics=[Metric(op="count")]), T, [])
    assert p["rows_matched"] == 8  # includes the 2026-06-30 23:59 row


def test_garbage_filter_value_is_rejected_not_parsed_as_a_tiny_number():
    with pytest.raises(PlanError, match="plain number"):
        run_plan(plan(filters=[Filter(column="amount_inr", op=">", value="They spent more than Rs. 10,000")], metrics=[Metric(op="count")]), T, [])


def test_amount_parsing_is_strict_and_understands_indian_units():
    from app.rag.aggregate import amounts_in, parse_amount
    assert parse_amount("Rs. 10,000") == 10000 and parse_amount("5 lakh") == 500000 and parse_amount("2cr") == 2e7 and parse_amount("1.5k") == 1500
    assert parse_amount("more than 10") is None and parse_amount("2026-01-01..2026-03-31") is None
    assert amounts_in("How many times did I spend more than Rs. 10,000 or 2 lakh in 2026?") == [10000.0, 200000.0]


def test_grouping_by_an_identifier_column_is_rejected():
    big = pd.DataFrame({"txn_id": [f"T{i}" for i in range(50)], "amt": ["1"] * 50})
    with pytest.raises(PlanError, match="almost every row is unique"):
        run_plan(AggPlan(is_aggregate=True, query=SubQuery(table="b", group_by=["txn_id"], metrics=[Metric(op="count")])), {"b": big}, [])


def test_month_trend_stops_at_the_last_month_of_data_and_counts_are_integers():
    p = run_plan(plan(date_column="timestamp", group_by=["month"], metrics=[Metric(op="count")]), T, periods("in 2026"))
    months = [r[0] for r in p["table"]["rows"]]
    assert months[-1] == "2026-09" and "2026-10" not in months and all(isinstance(r[1], int) for r in p["table"]["rows"])


def test_invented_average_is_ignored_unless_the_question_asks_for_one():
    q = dict(date_column="timestamp", group_by=["month"], metrics=[Metric(op="sum", column="amount_inr")], group_stat="mean_of_groups")
    assert not any("Average" in c["label"] for c in run_plan(plan(**q), T, periods("total in 2026"), "what is the total spend by month")["scalars"])
    assert any("Average" in c["label"] for c in run_plan(plan(**q), T, periods("in 2026"), "what is my average monthly spend")["scalars"])


# ---- plan repair: the exact bad plans the local 14B model produced against the real dataset -----------------------


def loans_accounts():
    loans = pd.DataFrame({"loan_id": [f"L{i}" for i in range(30)], "account_id": [f"A{i % 3}" for i in range(30)],
                          "start_date": ["2026-02-01"] * 15 + ["2025-11-01"] * 15, "principal_inr": ["100"] * 30, "status": ["Current"] * 30})
    accounts = pd.DataFrame({"account_id": ["A0", "A1", "A2"], "branch_id": ["BR1", "BR2", "BR2"]})
    return {"loans.csv": loans, "accounts.csv": accounts}


def test_repair_which_branch_adds_join_group_and_top1_and_comparison_works():
    bad = AggPlan(is_aggregate=True, compare_previous_period=True, query=SubQuery(table="loans.csv", date_column="start_date", metrics=[Metric(op="sum", column="principal_inr")]))
    p = run_plan(bad, loans_accounts(), periods("Which branch disbursed the highest loan volume in Q1 2026, and how does it compare to Q4 2025?"),
                 "Which branch disbursed the highest loan volume in Q1 2026, and how does it compare to Q4 2025?")
    got = dict(zip(p["table"]["columns"], p["table"]["rows"][0]))
    assert got["branch_id"] == "BR2" and got["sum_principal_inr"] == 1000 and got["prev_sum_principal_inr"] == 1000
    assert any("joined accounts.csv" in r for r in p["repairs"])


def test_repair_prose_filter_value_takes_the_amount_from_the_question_and_drops_id_grouping():
    bad = plan(date_column="timestamp", filters=[Filter(column="amount_inr", op=">", value="One transaction over 10000"), Filter(column="type", op="=", value="Debit")],
               group_by=["account_id"], metrics=[Metric(op="count", column="account_id")])
    q = "How many times did I spend more than Rs. 10,000 in a single transaction last quarter? Show the top 5."
    p = run_plan(bad, {"tx.csv": pd.concat([tx()] * 3, ignore_index=True)}, periods(q), q)
    assert cards(p)["Count of account id (Q3 2026)"] == 6  # 2 qualifying debits x 3 copies
    assert p["detail"] is not None and len(p["detail"]["rows"]) <= 5


def test_repair_trend_groups_by_month_sums_money_and_ignores_month_as_a_column():
    bad = plan(table="loans.csv", date_column="month", filters=[Filter(column="month", op="=", value="2026-02")], metrics=[Metric(op="count", column="loan_id")])
    q = "What is the month-wise trend of loan disbursements in 2026?"
    p = run_plan(bad, loans_accounts(), periods(q), q)
    assert p["table"]["columns"][0] == "month" and p["table"]["rows"][1][1] == 1500  # Feb 2026: 15 loans x 100, summed not counted


def test_repair_is_a_noop_for_a_correct_plan():
    from app.rag.aggregate import repair_plan
    good = plan(date_column="timestamp", group_by=["category"], metrics=[Metric(op="sum", column="amount_inr")])
    assert repair_plan(good, T, "total spend by category")[1] == []


def test_comparison_for_a_group_with_no_rows_in_the_other_period_is_zero_not_a_crash():
    loans = pd.DataFrame({"loan_id": ["a", "b"], "account_id": ["A0", "A1"], "start_date": ["2026-02-01", "2026-02-02"], "principal_inr": ["500", "100"]})
    accounts = pd.DataFrame({"account_id": ["A0", "A1"], "branch_id": ["BR1", "BR2"]})
    q = "Which branch disbursed the highest loan volume in Q1 2026, and how does it compare to Q4 2025?"
    p = run_plan(AggPlan(is_aggregate=True, compare_previous_period=True, query=SubQuery(table="loans", date_column="start_date", metrics=[Metric(op="sum", column="principal_inr")])),
                 {"loans": loans, "accounts": accounts}, periods(q), q)
    got = dict(zip(p["table"]["columns"], p["table"]["rows"][0]))
    assert got["branch_id"] == "BR1" and got["sum_principal_inr"] == 500 and got["prev_sum_principal_inr"] == 0 and got["change"] == 500
    assert any("no rows in Q4 2025" in n for n in p["notes"])


def test_time_word_used_as_a_metric_column_is_repaired():
    bad = plan(table="loans.csv", date_column="start_date", group_by=["month"], metrics=[Metric(op="count", column="month")])
    q = "What is the month-wise trend of loan disbursements in 2026?"
    p = run_plan(bad, loans_accounts(), periods(q), q)
    assert p["table"]["columns"][0] == "month" and p["table"]["rows"][1][1] == 1500
