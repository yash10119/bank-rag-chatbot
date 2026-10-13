import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..agents.helpers import redis_client
from ..agents.runtime import acquire_thread, config_for, pending_interrupt, release_thread, stream_turn
from ..db import SessionLocal, get_session
from ..deps import current_user, officer
from ..models import Approval, AuditEvent, Conversation, Document, Message, User
from ..queue import enqueue

log = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])
_tasks: set[asyncio.Task] = set()


class Send(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class Decision(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    note: str = Field("", max_length=500)


async def _owned(session: AsyncSession, user: User, cid: uuid.UUID) -> Conversation:
    conv = await session.get(Conversation, cid)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")  # never reveal other users' ids
    return conv


def _msg(m: Message) -> dict:
    return {"id": str(m.id), "role": m.role, "content": m.content, "trace": m.trace, "created_at": m.created_at.isoformat()}


@router.get("/conversations")
async def list_conversations(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Conversation).where(Conversation.user_id == user.id).order_by(Conversation.updated_at.desc()).limit(50))).scalars()
    return [{"id": str(c.id), "title": c.title, "updated_at": c.updated_at.isoformat()} for c in rows]


@router.post("/conversations")
async def new_conversation(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    conv = Conversation(user_id=user.id)
    session.add(conv)
    await session.commit()
    return {"id": str(conv.id), "title": conv.title}


@router.get("/conversations/{cid}")
async def get_conversation(cid: uuid.UUID, request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    conv = await _owned(session, user, cid)
    msgs = (await session.execute(select(Message).where(Message.conversation_id == cid).order_by(Message.created_at))).scalars()
    pending = await pending_interrupt(request.app.state.runtime, config_for(str(user.id), str(cid)))
    return {"id": str(conv.id), "title": conv.title, "messages": [_msg(m) for m in msgs], "pending": pending}


@router.delete("/conversations/{cid}")
async def delete_conversation(cid: uuid.UUID, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    conv = await _owned(session, user, cid)
    await session.delete(conv)
    await session.commit()
    return {"ok": True}


async def _save(cid: uuid.UUID, role: str, content: str, trace: dict | None = None, title: str | None = None) -> None:
    async with SessionLocal() as s:
        s.add(Message(conversation_id=cid, role=role, content=content, trace=trace))
        conv = await s.get(Conversation, cid)
        if conv:
            conv.updated_at = datetime.now(timezone.utc)
            if title and conv.title == "New conversation":
                conv.title = title[:60]
        await s.commit()


@router.post("/conversations/{cid}/messages")
async def send_message(cid: uuid.UUID, body: Send, request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """Server-sent events: `step` (agent activity), `token` (reply text), then `final` | `interrupt` | `error`.
    The run is detached from the HTTP connection so a closed browser tab cannot leave a half-written turn."""
    await _owned(session, user, cid)
    uid = str(user.id)
    if not await acquire_thread(uid, str(cid)):
        raise HTTPException(409, "This conversation is still processing your previous message")
    rt = request.app.state.runtime
    queue: asyncio.Queue = asyncio.Queue()
    identity = {"id": uid, "name": user.name, "email": user.email}

    async def runner() -> None:
        try:
            await _save(cid, "user", body.text, title=body.text)
            async for ev in stream_turn(rt, user=identity, conversation_id=str(cid), text=body.text):
                if ev["type"] == "final":
                    await _save(cid, "assistant", ev["answer"], {"steps": ev["trace"], "actions": ev["actions"], "analytics": ev.get("analytics"), "sources": ev.get("sources")})
                elif ev["type"] == "interrupt":
                    await _save(cid, "assistant", ev["message"], {"interrupt": ev["interrupt"]["type"]})
                await queue.put(ev)
        except Exception:
            log.exception("chat turn failed")
            await queue.put({"type": "error", "message": "Something went wrong while processing your request. Please try again."})
        finally:
            await release_thread(uid, str(cid))
            await queue.put(None)

    task = asyncio.create_task(runner())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)

    async def events():
        while (ev := await queue.get()) is not None:
            yield f"data: {json.dumps(ev, default=str)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ activity


@router.get("/activity")
async def activity(user: User = Depends(current_user), session: AsyncSession = Depends(get_session), all: bool = False,
                   kind: str | None = None, limit: int = 100):
    """Queryable audit trail. Customers see their own events; officers can pass all=true. Filter with ?kind=mcp_tool_call etc."""
    q = select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(min(max(limit, 1), 500))
    if not (all and user.role == "officer"):
        q = q.where(AuditEvent.user_id == user.id)
    if kind:
        q = q.where(AuditEvent.kind.in_(kind.split(",")))
    rows = (await session.execute(q)).scalars()
    return [{"id": str(a.id), "kind": a.kind, "payload": a.payload, "at": a.created_at.isoformat(), "mine": a.user_id == user.id} for a in rows]


@router.get("/monitor")
async def monitor(user: User = Depends(officer), session: AsyncSession = Depends(get_session)):
    """Operations view for bank staff: what is running now and what happened in the last 24h."""
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    counts = dict((await session.execute(
        select(AuditEvent.kind, func.count()).where(AuditEvent.created_at >= since).group_by(AuditEvent.kind))).all())
    recent = (await session.execute(select(AuditEvent).where(AuditEvent.kind.in_(["mcp_tool_call", "oauth_connected", "oauth_refreshed",
              "oauth_refresh_failed", "oauth_disconnected", "file_rejected"])).order_by(AuditEvent.created_at.desc()).limit(25))).scalars()
    llm_rows = (await session.execute(select(AuditEvent.payload).where(AuditEvent.kind == "llm_call", AuditEvent.created_at >= since).limit(500))).scalars().all()
    ms = [p.get("ms", 0) for p in llm_rows if p.get("ms")]
    mcp_rows = (await session.execute(select(AuditEvent.payload).where(AuditEvent.kind == "mcp_tool_call", AuditEvent.created_at >= since).limit(1000))).scalars().all()
    per_tool: dict[str, dict] = {}
    for p in mcp_rows:
        t = per_tool.setdefault(f"{p.get('service')}.{p.get('tool')}", {"calls": 0, "failed": 0})
        t["calls"] += 1
        t["failed"] += 0 if p.get("ok") else 1
    r = redis_client()
    active = [k.decode() if isinstance(k, bytes) else k async for k in r.scan_iter("lock:*")]
    docs = dict((await session.execute(select(Document.status, func.count()).group_by(Document.status))).all())
    pending = (await session.execute(select(func.count()).select_from(Approval).where(Approval.status == "pending"))).scalar_one()
    return {
        "events_24h": counts, "active_runs": [a.removeprefix("lock:") for a in active], "queue_depth": await r.zcard("arq:queue"),
        "documents": docs, "pending_approvals": pending,
        "llm": {"calls_24h": len(ms), "avg_ms": int(sum(ms) / len(ms)) if ms else 0, "max_ms": max(ms) if ms else 0},
        "mcp_by_tool": per_tool,
        "recent": [{"kind": a.kind, "payload": a.payload, "at": a.created_at.isoformat()} for a in recent],
    }


# ----------------------------------------------------------------- approvals


def _appr(a: Approval, requester: str = "") -> dict:
    return {"id": str(a.id), "status": a.status, "reasons": a.reasons, "summary": a.summary, "requester": requester,
            "created_at": a.created_at.isoformat(), "note": a.note, "mine": False}


@router.get("/approvals")
async def approvals(status: str = "pending", user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """Officers see all requests; customers see only their own (read-only)."""
    q = select(Approval, User.email).join(User, User.id == Approval.user_id).order_by(Approval.created_at.desc()).limit(100)
    if status != "all":
        q = q.where(Approval.status == status)
    if user.role != "officer":
        q = q.where(Approval.user_id == user.id)
    rows = (await session.execute(q)).all()
    return [{**_appr(a, email), "mine": a.user_id == user.id} for a, email in rows]


@router.post("/approvals/{aid}/decision")
async def decide(aid: uuid.UUID, body: Decision, user: User = Depends(officer), session: AsyncSession = Depends(get_session)):
    appr = await session.get(Approval, aid)
    if appr is None:
        raise HTTPException(404, "Approval not found")
    if appr.user_id == user.id:
        raise HTTPException(403, "Four-eyes rule: you cannot decide your own request")
    res = await session.execute(
        update(Approval).where(Approval.id == aid, Approval.status == "pending")
        .values(status=body.decision, decided_by=user.id, note=body.note, decided_at=datetime.now(timezone.utc))
    )
    await session.commit()
    if res.rowcount != 1:
        raise HTTPException(409, "This request was already decided")
    session.add(AuditEvent(user_id=appr.user_id, conversation_id=appr.conversation_id, kind="approval_decided",
                           payload={"approval_id": str(aid), "decision": body.decision, "officer": str(user.id)}))
    await session.commit()
    await enqueue("resume_after_approval", str(aid))
    return {"ok": True, "status": body.decision}
