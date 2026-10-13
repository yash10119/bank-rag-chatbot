from datetime import datetime
from zoneinfo import ZoneInfo

from app.agents.helpers import resolve_relative_date

MON = datetime(2026, 10, 12, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))  # a Monday
THU = datetime(2026, 10, 15, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))


def test_this_thursday_from_monday():
    assert resolve_relative_date("schedule a call with my RM this Thursday", MON) == "2026-10-15"


def test_bare_weekday_and_case():
    assert resolve_relative_date("Can we meet on FRIDAY?", MON) == "2026-10-16"


def test_today_counts_for_this_but_not_next():
    assert resolve_relative_date("this thursday", THU) == "2026-10-15"
    assert resolve_relative_date("next thursday", THU) == "2026-10-22"


def test_tomorrow_and_day_after():
    assert resolve_relative_date("call me tomorrow", MON) == "2026-10-13"
    assert resolve_relative_date("the day after tomorrow", MON) == "2026-10-14"


def test_no_relative_day():
    assert resolve_relative_date("what is the home loan rate", MON) is None
