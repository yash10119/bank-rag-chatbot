"""Google Calendar MCP server (streamable HTTP). Acts as the verified calling user."""
import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from ..config import settings
from . import mock
from .common import authed, bearer, check_emails, raise_for_google, register_oauth

API = "https://www.googleapis.com/calendar/v3"
mcp = FastMCP("bank-calendar", host="0.0.0.0", port=settings.mcp_port, stateless_http=True, json_response=True)
register_oauth(mcp, "calendar")


def _parse(iso: str) -> datetime:
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise ToolError(f"invalid datetime: {iso!r}") from exc
    if dt.tzinfo is None:
        raise ToolError("datetime must include a UTC offset")
    return dt


async def _busy(client: httpx.AsyncClient, token: str, start: datetime, end: datetime, attendees: list[str], uid: str = ""):
    """Returns (busy intervals, attendees whose calendars could not be read)."""
    if settings.mcp_mock:
        return await mock.busy(uid, start, end), []
    items = [{"id": "primary"}] + [{"id": a} for a in attendees]
    r = await client.post(
        f"{API}/freeBusy",
        json={"timeMin": start.isoformat(), "timeMax": end.isoformat(), "items": items},
        headers=bearer(token),
    )
    raise_for_google(r)
    busy, unknown = [], []
    for cal_id, info in r.json().get("calendars", {}).items():
        if info.get("errors"):
            if cal_id != "primary":
                unknown.append(cal_id)
            continue
        busy += [(_parse(b["start"]), _parse(b["end"])) for b in info.get("busy", [])]
    return busy, unknown


@mcp.tool()
async def check_availability(ctx: Context, start: str, end: str, attendees: list[str] | None = None) -> dict:
    """Is the user's calendar (and any readable attendee calendars) free in [start, end)?
    `unreadable` lists attendees whose calendars could not be checked."""
    uid, token = await authed(ctx, "calendar")
    s, e = _parse(start), _parse(end)
    async with httpx.AsyncClient(timeout=20) as c:
        busy, unknown = await _busy(c, token, s, e, check_emails(attendees or []), uid)
    return {
        "ok": True,
        "free": not busy,
        "busy": [{"start": a.isoformat(), "end": b.isoformat()} for a, b in busy],
        "unreadable": unknown,
    }


@mcp.tool()
async def find_free_slots(
    ctx: Context,
    day: str,
    timezone: str,
    duration_minutes: int = 30,
    attendees: list[str] | None = None,
    earliest_hour: int = 9,
    latest_hour: int = 17,
    max_slots: int = 3,
) -> dict:
    """Free slots on `day` (YYYY-MM-DD) within working hours, at 30-minute steps."""
    uid, token = await authed(ctx, "calendar")
    try:
        tz = ZoneInfo(timezone)
        base = datetime.fromisoformat(day).replace(tzinfo=tz)
    except Exception as exc:
        raise ToolError("invalid day or timezone") from exc
    win_start = base.replace(hour=earliest_hour, minute=0)
    win_end = base.replace(hour=latest_hour, minute=0)
    async with httpx.AsyncClient(timeout=20) as c:
        busy, unknown = await _busy(c, token, win_start, win_end, check_emails(attendees or []), uid)
    step, dur = timedelta(minutes=30), timedelta(minutes=duration_minutes)
    slots, cur = [], win_start
    now = datetime.now(tz)
    while cur + dur <= win_end and len(slots) < max_slots:
        if cur > now and all(cur + dur <= b0 or cur >= b1 for b0, b1 in busy):
            slots.append({"start": cur.isoformat(), "end": (cur + dur).isoformat()})
        cur += step
    return {"ok": True, "slots": slots, "unreadable": unknown}


@mcp.tool()
async def create_event(
    ctx: Context,
    summary: str,
    start: str,
    end: str,
    timezone: str,
    description: str,
    location: str,
    attendees: list[str],
    reference: str,
) -> dict:
    """Create an event and email invites. Re-checks availability itself and refuses to
    double-book. The event id is derived from `reference`, so retries are idempotent."""
    uid, token = await authed(ctx, "calendar")
    s, e = _parse(start), _parse(end)
    emails = check_emails(attendees)
    event_id = hashlib.sha1(reference.encode()).hexdigest()[:26]  # base32hex-compatible
    if settings.mcp_mock:
        if existing := await mock.event_by_id(uid, event_id):  # idempotent retry
            return {"ok": True, "event_id": event_id, "html_link": f"mock://calendar/{event_id}", "reused": True, "mock": True}
        if busy := await mock.busy(uid, s, e):
            return {"ok": False, "error": "conflict", "busy": [{"start": a.isoformat(), "end": b.isoformat()} for a, b in busy]}
        return await mock.create_event(uid, event_id, {
            "summary": summary[:200], "start": s.isoformat(), "end": e.isoformat(), "timezone": timezone,
            "description": description[:4000], "location": location[:300], "attendees": emails, "reference": reference,
        })
    async with httpx.AsyncClient(timeout=20) as c:
        busy, _ = await _busy(c, token, s, e, [a for a in emails])
        if busy:
            # An earlier identical attempt may have created the event; that's not a conflict.
            existing = await c.get(f"{API}/calendars/primary/events/{event_id}", headers=bearer(token))
            if existing.status_code == 200 and existing.json().get("status") != "cancelled":
                return {"ok": True, "event_id": event_id, "html_link": existing.json().get("htmlLink"), "reused": True}
            return {"ok": False, "error": "conflict", "busy": [{"start": a.isoformat(), "end": b.isoformat()} for a, b in busy]}
        body = {
            "id": event_id,
            "summary": summary[:200],
            "description": description[:4000],
            "location": location[:300],
            "start": {"dateTime": s.isoformat(), "timeZone": timezone},
            "end": {"dateTime": e.isoformat(), "timeZone": timezone},
            "attendees": [{"email": a} for a in emails],
            "extendedProperties": {"private": {"bankReference": reference}},
            "reminders": {"useDefault": True},
        }
        r = await c.post(
            f"{API}/calendars/primary/events", params={"sendUpdates": "all"}, json=body, headers=bearer(token)
        )
    raise_for_google(r)
    ev = r.json()
    return {"ok": True, "event_id": ev["id"], "html_link": ev.get("htmlLink")}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
