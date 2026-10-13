"""Mock backends for the Gmail / Calendar / Slack MCP servers (MCP_MOCK=true).

Same tool signatures and result shapes as the real servers, but state lives in the
`mock_items` table, always keyed by the verified user id. Nothing leaves the machine.
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..db import SessionLocal
from ..models import MockItem

DEMO_INBOX = [
    ("Priya Sharma <rm@example.com>", "Your home loan documents", "Hi, please share your last 3 salary slips so I can move your home loan file forward."),
    ("Demo Retail Bank <alerts@demobank.example>", "Statement ready", "Your account statement for last month is now available in net banking."),
    ("Demo Retail Bank <offers@demobank.example>", "FD special rates this week", "Book a 1-year fixed deposit this week and earn a special rate."),
]


async def add(user_id: str, service: str, kind: str, payload: dict) -> MockItem:
    async with SessionLocal() as s:
        item = MockItem(user_id=uuid.UUID(user_id), service=service, kind=kind, payload=payload)
        s.add(item)
        await s.commit()
        await s.refresh(item)
        return item


async def items(user_id: str, service: str, kinds: tuple[str, ...] | None = None, limit: int = 200) -> list[MockItem]:
    q = select(MockItem).where(MockItem.user_id == uuid.UUID(user_id), MockItem.service == service)
    if kinds:
        q = q.where(MockItem.kind.in_(kinds))
    async with SessionLocal() as s:
        return list((await s.execute(q.order_by(MockItem.created_at.desc()).limit(limit))).scalars())


# ------------------------------------------------------------------ gmail


async def send_email(user_id: str, to: list[str], subject: str, body: str, cc: list[str] | None) -> dict:
    item = await add(user_id, "gmail", "sent", {"to": to, "cc": cc or [], "subject": subject[:200], "body": body})
    return {"ok": True, "message_id": str(item.id), "mock": True}


async def _ensure_inbox(user_id: str) -> None:
    if await items(user_id, "gmail", ("inbox",), limit=1):
        return
    now = datetime.now(timezone.utc)
    for i, (sender, subject, body) in enumerate(DEMO_INBOX):
        await add(user_id, "gmail", "inbox", {"from": sender, "subject": subject, "body": body, "date": (now - timedelta(days=i + 1)).isoformat()})


async def list_emails(user_id: str, query: str, max_results: int) -> dict:
    await _ensure_inbox(user_id)
    words = [w.lower() for w in query.split() if ":" not in w]  # Gmail operators (newer_than:7d) are ignored
    out = []
    for it in await items(user_id, "gmail", ("inbox",)):
        p = it.payload
        if words and not all(w in f"{p['from']} {p['subject']} {p['body']}".lower() for w in words):
            continue
        out.append({"id": str(it.id), "from": p["from"], "subject": p["subject"], "date": p["date"], "snippet": p["body"][:120]})
    return {"ok": True, "emails": out[:max_results], "mock": True}


async def read_email(user_id: str, message_id: str) -> dict:
    for it in await items(user_id, "gmail", ("inbox", "sent")):
        if str(it.id) == message_id:
            p = it.payload
            return {"ok": True, "from": p.get("from", "me"), "subject": p["subject"], "date": p.get("date", it.created_at.isoformat()), "body": p["body"][:4000]}
    return {"ok": False, "error": "message_not_found"}


# --------------------------------------------------------------- calendar


async def busy(user_id: str, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    out = []
    for it in await items(user_id, "calendar", ("event",)):
        s, e = datetime.fromisoformat(it.payload["start"]), datetime.fromisoformat(it.payload["end"])
        if s < end and e > start:
            out.append((s, e))
    return out


async def event_by_id(user_id: str, event_id: str) -> MockItem | None:
    for it in await items(user_id, "calendar", ("event",)):
        if it.payload.get("event_id") == event_id:
            return it
    return None


async def create_event(user_id: str, event_id: str, payload: dict) -> dict:
    await add(user_id, "calendar", "event", {**payload, "event_id": event_id})
    return {"ok": True, "event_id": event_id, "html_link": f"mock://calendar/{event_id}", "mock": True}


# ------------------------------------------------------------------ slack


async def post(user_id: str, kind: str, payload: dict) -> dict:
    item = await add(user_id, "slack", kind, payload)
    return {"ok": True, "channel": payload.get("channel"), "ts": str(item.created_at.timestamp()), "mock": True}
