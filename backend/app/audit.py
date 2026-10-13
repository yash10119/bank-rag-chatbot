"""Structured, queryable audit trail (banking compliance): every significant action becomes one `audit_events` row.

Kinds: file_uploaded | file_rejected | file_indexed | file_failed | agent_turn | mcp_tool_call | llm_call |
       oauth_connected | oauth_failed | oauth_disconnected | oauth_refreshed | oauth_refresh_failed |
       approval_decided | query_resolved

Rows are append-only JSON (kind, user, conversation, payload, timestamp), indexed by kind and time, so they can
be queried through GET /activity?kind=... and summarised by GET /monitor. Payloads never contain tokens or
secrets; LLM prompts/responses are truncated.
"""
import logging
import time
import uuid
from contextvars import ContextVar
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler

from .db import SessionLocal
from .models import AuditEvent

log = logging.getLogger("audit")

# Set per request/turn by the API and worker so background LLM/MCP calls are attributed to the right user.
audit_ctx: ContextVar[dict] = ContextVar("audit_ctx", default={})
MAX_TEXT = 2500


def _uuid(v) -> UUID | None:
    try:
        return v if isinstance(v, UUID) or v is None else UUID(str(v))
    except ValueError:
        return None


async def log_event(kind: str, *, user_id=None, conversation_id=None, **payload: Any) -> None:
    """Append one audit row. Never raises: auditing must not break the user's request."""
    ctx = audit_ctx.get()
    try:
        async with SessionLocal() as s:
            s.add(AuditEvent(user_id=_uuid(user_id or ctx.get("user_id")), conversation_id=_uuid(conversation_id or ctx.get("conversation_id")),
                             kind=kind, payload=payload))
            await s.commit()
    except Exception:  # pragma: no cover - best effort
        log.exception("could not write audit event %s", kind)


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + f"... [{len(text) - MAX_TEXT} more chars]"


class AuditCallback(AsyncCallbackHandler):
    """Logs every LLM prompt and response (with latency) as an `llm_call` event for the current user."""

    raise_error = False

    def __init__(self, model: str):
        self.model = model
        self._start: dict[uuid.UUID, tuple[float, str]] = {}

    async def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        prompt = "\n".join(f"[{m.type}] {m.content}" for m in messages[0])
        self._start[run_id] = (time.perf_counter(), _clip(prompt))

    async def on_llm_end(self, response, *, run_id, **kwargs):
        t0, prompt = self._start.pop(run_id, (time.perf_counter(), ""))
        try:
            text = response.generations[0][0].text
        except Exception:
            text = ""
        await log_event("llm_call", model=self.model, prompt=prompt, response=_clip(text), ms=int((time.perf_counter() - t0) * 1000))

    async def on_llm_error(self, error, *, run_id, **kwargs):
        t0, prompt = self._start.pop(run_id, (time.perf_counter(), ""))
        await log_event("llm_call", model=self.model, prompt=prompt, error=type(error).__name__, ms=int((time.perf_counter() - t0) * 1000))
