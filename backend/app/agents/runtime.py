"""Compiled graph + Postgres checkpointer shared by the API and the worker.

Isolation: every conversation is its own LangGraph thread keyed `<user_id>:<conversation_id>`
and the thread id is built server-side from the authenticated user, never from client input."""
import uuid
from dataclasses import dataclass

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from ..audit import audit_ctx
from ..config import settings
from .graph import build_graph
from .helpers import redis_client


@dataclass
class Runtime:
    """The compiled graph plus the Postgres connection pool behind its checkpointer."""
    graph: object
    pool: AsyncConnectionPool


_runtime: Runtime | None = None


async def init_runtime() -> Runtime:
    """Build the graph once per process with a Postgres checkpointer, so conversations and paused
    approvals survive restarts and can be resumed by a different process (API or worker).
    """
    global _runtime
    if _runtime is None:
        pool = AsyncConnectionPool(
            settings.psycopg_url, max_size=20, open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        await pool.open()
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        _runtime = Runtime(build_graph(saver), pool)
    return _runtime


async def close_runtime() -> None:
    """Close the checkpointer pool on shutdown."""
    global _runtime
    if _runtime:
        await _runtime.pool.close()
        _runtime = None


def thread_id(user_id: str, conversation_id: str) -> str:
    """LangGraph thread id. Prefixing with the user id means one user's conversation id can never address
    another user's checkpoints.
    """
    return f"{user_id}:{conversation_id}"


def config_for(user_id: str, conversation_id: str) -> dict:
    """Graph run config for one conversation. The recursion limit bounds clarify loops."""
    return {"configurable": {"thread_id": thread_id(user_id, conversation_id)}, "recursion_limit": 40}


async def pending_interrupt(rt: Runtime, cfg: dict) -> dict | None:
    """Return the payload of the interrupt this conversation is paused on (clarification or approval), if any."""
    snap = await rt.graph.aget_state(cfg)
    for task in snap.tasks:
        for intr in task.interrupts:
            return intr.value
    return None


async def acquire_thread(user_id: str, conversation_id: str, ttl: int = 180) -> bool:
    """One in-flight run per conversation (prevents interleaved checkpoints)."""
    return bool(await redis_client().set(f"lock:{thread_id(user_id, conversation_id)}", "1", nx=True, ex=ttl))


async def release_thread(user_id: str, conversation_id: str) -> None:
    """Release the per-conversation lock taken by acquire_thread."""
    await redis_client().delete(f"lock:{thread_id(user_id, conversation_id)}")


def final_payload(values: dict) -> dict:
    """What the UI needs after a turn: reply, agent trace, MCP actions, the exact aggregate result (tables/cards)
    and the source files/row ranges the answer was grounded in."""
    evidence = values.get("evidence") or {}
    analytics = (evidence.get("analytics_agent") or {}).get("result")
    srcs: list[dict] = []
    for agent, ev in evidence.items():
        for s in ev.get("sources") or []:
            if isinstance(s, dict):
                srcs.append({"file": s.get("file"), "sheet": s.get("sheet"), "rows": s.get("rows"), "agent": agent})
            else:  # analytics: whole-table exact computation
                srcs.append({"file": s, "sheet": None, "rows": "all matching rows (exact computation)", "agent": agent})
    return {"answer": values.get("answer", ""), "trace": values.get("trace", []), "actions": values.get("actions", []),
            "analytics": analytics, "sources": srcs[:12]}


async def stream_turn(rt: Runtime, *, user: dict, conversation_id: str, text: str):
    """Async generator of UI events for one user message. Resumes a clarification
    interrupt when one is pending, otherwise starts a fresh turn."""
    cfg = config_for(user["id"], conversation_id)
    audit_ctx.set({"user_id": user["id"], "conversation_id": conversation_id})  # LLM/MCP audit rows get this user
    pending = await pending_interrupt(rt, cfg)
    if pending and pending["type"] == "approval":
        yield {"type": "interrupt", "interrupt": pending,
               "message": "Your earlier request is waiting for a bank officer's review. You'll see the outcome here once it's decided."}
        return
    if pending:  # clarification: the new message IS the answer
        graph_input = Command(resume=text)
    else:
        graph_input = {
            "messages": [HumanMessage(content=text)],
            "user_id": user["id"], "user_name": user["name"], "user_email": user["email"],
            "conversation_id": conversation_id, "turn_id": uuid.uuid4().hex, "clarifications": 0,
        }
    async for mode, chunk in rt.graph.astream(graph_input, cfg, stream_mode=["custom"]):
        yield chunk
    snap = await rt.graph.aget_state(cfg)
    for task in snap.tasks:
        for intr in task.interrupts:
            yield {"type": "interrupt", "interrupt": intr.value,
                   "message": intr.value.get("question") if intr.value["type"] == "clarification"
                   else "This request needs a bank officer's approval. I've escalated it; you'll be notified here."}
            return
    values = snap.values
    yield {"type": "final", **final_payload(values)}


async def resume_with_decision(rt: Runtime, *, user_id: str, conversation_id: str, decision: dict) -> dict:
    """Used by the worker after an officer decides: resumes the paused thread to completion."""
    cfg = config_for(user_id, conversation_id)
    audit_ctx.set({"user_id": user_id, "conversation_id": conversation_id})
    async for _ in rt.graph.astream(Command(resume=decision), cfg, stream_mode=["custom"]):
        pass
    return final_payload((await rt.graph.aget_state(cfg)).values)
