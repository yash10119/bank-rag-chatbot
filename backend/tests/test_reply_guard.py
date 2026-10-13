"""The reply guard removes contact details the model invented and leaves grounded content alone."""
from app.agents.helpers import strip_ungrounded_contacts, ungrounded_contacts

FACTS = (
    '{"appointment": {"reference": "APT-20261014-B74A", "when": "Wednesday 14 Oct 2026, 09:00 UTC+05:30"}, '
    '"findings": "Apr-Jun debit 48,190,492.81 across 931 rows", "rm_email": "rm@example.com", '
    '"branch_phone": "+91 22 5550 1234"} yash@example.com Main Branch, 1 Banking Plaza, Mumbai 400001'
)


def test_invented_hotline_sentence_is_removed():
    reply = ("Thank you for reporting this.\n\n"
             "If you need to block your card, please contact our customer service hotline at 123-456-7890.\n\n"
             "Best regards,\nDemo Retail Bank Team")
    clean, removed = strip_ungrounded_contacts(reply, FACTS)
    assert removed == ["123-456-7890"]
    assert "123-456-7890" not in clean and "hotline" not in clean
    assert clean.startswith("Thank you for reporting this.") and clean.endswith("Demo Retail Bank Team")


def test_only_the_offending_sentence_of_a_line_goes():
    clean, removed = strip_ungrounded_contacts("- Your meeting is confirmed. Email help@madeup-bank.com for changes.", FACTS)
    assert removed == ["help@madeup-bank.com"]
    assert clean == "- Your meeting is confirmed."


def test_bullet_left_empty_is_dropped():
    clean, _ = strip_ungrounded_contacts("Done:\n- Team notified.\n- Visit https://fake.example/track for status.", FACTS)
    assert clean == "Done:\n- Team notified."


def test_grounded_details_are_kept():
    reply = ("Your appointment APT-20261014-B74A is on Wednesday 14 Oct 2026, 09:00 UTC+05:30 at Main Branch, 1 Banking Plaza, "
             "Mumbai 400001. Total debit was 48,190,492.81 over 931 rows. Your manager is rm@example.com, phone +91 22 5550 1234. "
             "A copy went to yash@example.com.")
    assert strip_ungrounded_contacts(reply, FACTS) == (reply, [])


def test_amounts_dates_and_rates_are_not_mistaken_for_phone_numbers():
    reply = "Rate 8.35% for 5 years; Rs. 10,746,174.83 outstanding; 1,234,567,890.50 total; between 2026-04-01 and 2026-06-30; score 750-799."
    assert ungrounded_contacts(reply, "") == []
