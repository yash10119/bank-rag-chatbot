"""arq worker: document ingestion, resume-after-officer-decision, audit mirroring to Slack."""
import logging
import uuid
from datetime import datetime, timezone

from arq import Retry
from sqlalchemy import select

from .agents.runtime import acquire_thread, close_runtime, init_runtime, release_thread, resume_with_decision
from .db import SessionLocal
from .mcp_client import call_tool
from .models import Approval, Conversation, Message
from .queue import redis_settings
from .rag import store
from .rag.ingest import ingest_document

log = logging.getLogger("worker")


async def ingest_document_job(ctx, doc_id: str):
    return await ingest_document(doc_id)


async def resume_after_approval(ctx, approval_id: str):
    rt = ctx["runtime"]
    async with SessionLocal() as s:
        appr = (await s.execute(select(Approval).where(Approval.id == uuid.UUID(approval_id)))).scalar_one()
    uid, cid = str(appr.user_id), str(appr.conversation_id)
    if not await acquire_thread(uid, cid, ttl=300):
        raise Retry(defer=10)  # conversation busy; try again shortly
    try:
        result = await resume_with_decision(
            rt, user_id=uid, conversation_id=cid,
            decision={"decision": appr.status, "note": appr.note or "", "officer": str(appr.decided_by)},
        )
        content, trace = result["answer"], {"steps": result["trace"], "actions": result["actions"], "analytics": result.get("analytics"), "sources": result.get("sources")}
    except Exception:
        log.exception("resume failed for approval %s", approval_id)
        content, trace = "A bank officer reviewed your request, but I couldn't complete the follow-up automatically. A team member will contact you.", None
    finally:
        await release_thread(uid, cid)
    async with SessionLocal() as s:
        s.add(Message(conversation_id=appr.conversation_id, role="assistant", content=content, trace=trace))
        conv = await s.get(Conversation, appr.conversation_id)
        if conv:
            conv.updated_at = datetime.now(timezone.utc)
        await s.commit()


async def post_audit(ctx, user_id: str, channel: str, summary: dict):
    """Mirror the structured audit summary to Slack using the customer's own Slack connection (if any)."""
    fields = {
        "Intents": ", ".join(summary.get("intents") or []) or "-",
        "Risk": str(summary.get("risk")),
        "Approval": str(summary.get("approval") or "n/a"),
        "Agents": ", ".join(summary.get("agents") or []) or "-",
        "Actions": "; ".join(f"{a['service']}:{a['kind']}={a['status']}" for a in summary.get("actions", [])) or "none",
        "Turn": str(summary.get("turn_id", ""))[:12],
    }
    res = await call_tool("slack", user_id, "post_structured", {"channel": channel, "title": "Customer query resolved", "fields": fields, "severity": "info"})
    if not res.ok and not res.not_connected:
        log.warning("audit slack post failed: %s", res.error)


async def startup(ctx):
    logging.basicConfig(level=logging.INFO)
    await store.ensure_collection()
    ctx["runtime"] = await init_runtime()


async def shutdown(ctx):
    await close_runtime()


class WorkerSettings:
    functions = [ingest_document_job, resume_after_approval, post_audit]
    redis_settings = redis_settings()
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 4
    job_timeout = 600
    max_tries = 3
