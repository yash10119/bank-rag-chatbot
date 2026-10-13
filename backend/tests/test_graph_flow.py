"""End-to-end graph tests with the LLM, RAG, DB and MCP layers faked.
They verify ROUTING, parallel fan-out, HITL pause/resume, and per-user identity on every MCP call."""
import asyncio
import uuid
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.agents import actions, core, specialists
from app.agents.core import Threshold, Understanding
from app.agents.graph import build_graph
from app.agents.specialists import Eligibility
from app.mcp_client import McpResult

CID = "00000000-0000-0000-0000-0000000000c1"
PROFILE = {"credit_score": 782, "rm_email": "rm@bank.test", "relationship_manager_name": "Priya"}


class FakeStructured:
    def __init__(self, schema):
        self.schema = schema

    async def ainvoke(self, msgs):
        if self.schema is Understanding:
            text = " ".join(str(getattr(m, "content", m)) for m in msgs[1:]).lower()
            if "transfer" in text:
                return Understanding(intents=["fund_transfer"], amount=50000.0)
            return Understanding(
                intents=["rate_inquiry", "eligibility_check", "schedule_appointment"], loan_type="Home Loan",
                tenure_years=5, credit_score=751, appointment_date="2999-01-04", appointment_time=None,
                appointment_topic="Home loan discussion",
            )
        if self.schema is Eligibility:
            return Eligibility(eligible=True, summary="Meets all rules", checks=["score 782 >= 650"])
        if self.schema is Threshold:
            return Threshold(value=10000.0)
        raise AssertionError(self.schema)


class FakeLLM:
    def with_structured_output(self, schema):
        return FakeStructured(schema)

    async def ainvoke(self, msgs):
        return AIMessage(content="Home Loan 5y @ 8.35% [S1]")

    async def astream(self, msgs):
        for piece in ("Your rate ", "is 8.35%."):
            yield AIMessageChunk(content=piece)


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, model, pk):
        return SimpleNamespace(timezone="Asia/Kolkata", prefs={})

    def add(self, obj):
        if hasattr(obj, "reasons") and getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()

    async def commit(self):
        pass


@pytest.fixture
def env(monkeypatch):
    calls: list[tuple[str, str, str, dict]] = []

    async def fake_call(service, user_id, tool, args):
        calls.append((service, user_id, tool, args))
        data = {"ok": True}
        if tool == "check_availability":
            data["free"] = True
        if tool == "find_free_slots":
            data["slots"] = [{"start": "2999-01-04T10:00:00+05:30", "end": "2999-01-04T10:30:00+05:30"}]
        if tool == "create_event":
            data.update(event_id="ev1", html_link="http://cal/ev1")
        return McpResult(True, data)

    async def fake_retrieve(*a, **k):
        return [{"text": "[rates.csv > csv] product: Home Loan; tenure_years: 5; min_credit_score: 750; interest_rate_pct: 8.35",
                 "filename": "rates.csv", "sheet": "csv", "row_start": 3, "row_end": 3}]

    async def fake_conn_status(session, uid):
        return {s: {"connected": True} for s in ("gmail", "calendar", "slack")}

    async def fake_profile(uid):
        return PROFILE

    async def fake_analyze(uid, thr):
        return None

    async def _true(*a, **k):
        return True

    async def _noop(*a, **k):
        return None

    for mod in (core, specialists, actions):
        if hasattr(mod, "call_tool"):
            monkeypatch.setattr(mod, "call_tool", fake_call)
        if hasattr(mod, "retrieve"):
            monkeypatch.setattr(mod, "retrieve", fake_retrieve)
        if hasattr(mod, "llm"):
            monkeypatch.setattr(mod, "llm", lambda: FakeLLM())
        if hasattr(mod, "SessionLocal"):
            monkeypatch.setattr(mod, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(core, "connection_status", fake_conn_status)
    monkeypatch.setattr(core, "load_profile", fake_profile)
    monkeypatch.setattr(core, "enqueue", _noop)
    monkeypatch.setattr(specialists, "analyze_transactions", fake_analyze)

    async def fake_series(uid, question):
        return []

    monkeypatch.setattr(specialists, "analyze_series", fake_series)
    monkeypatch.setattr(actions, "_reserve", _true)
    monkeypatch.setattr(actions, "_finish_reservation", _noop)
    monkeypatch.setattr(actions, "once", _true)
    return SimpleNamespace(calls=calls, graph=build_graph(MemorySaver()))


def first_input(uid: str, text: str, cid: str = CID) -> dict:
    return {"messages": [HumanMessage(text)], "user_id": uid, "user_name": f"Name-{uid}", "user_email": f"{uid}@x.test",
            "conversation_id": cid, "turn_id": uuid.uuid4().hex, "clarifications": 0}


def cfg(uid: str, cid: str = CID) -> dict:
    return {"configurable": {"thread_id": f"{uid}:{cid}"}, "recursion_limit": 40}


async def test_user_story_runs_parallel_agents_and_all_three_mcp_services(env):
    uid = str(uuid.uuid4())
    out = await env.graph.ainvoke(first_input(uid, "5y home loan rate above 750 and schedule a call Thursday"), cfg(uid))
    ran = {t["agent"] for t in out["trace"]}
    assert {"intake", "rates_agent", "eligibility_agent", "scheduler", "notify", "respond", "audit"} <= ran
    assert out["appointment"]["status"] == "confirmed"
    services = {c[0] for c in env.calls}
    assert services == {"calendar", "gmail", "slack"}
    assert all(c[1] == uid for c in env.calls), "every MCP call must carry the state's user id"
    tools = [c[2] for c in env.calls]
    assert tools.index("create_event") < tools.index("send_email")  # booking happens before confirmation
    assert out["answer"] == "Your rate is 8.35%."
    assert env.calls[[c[2] for c in env.calls].index("send_email")][3]["to"] == [f"{uid}@x.test"]


async def test_concurrent_users_do_not_leak(env):
    users = [str(uuid.uuid4()) for _ in range(5)]
    outs = await asyncio.gather(*(env.graph.ainvoke(first_input(u, "rate and call"), cfg(u)) for u in users))
    for u, out in zip(users, outs):
        assert out["user_id"] == u and out["user_email"] == f"{u}@x.test"
        assert all(m.content != "" for m in out["messages"])
    by_user = {u: [c for c in env.calls if c[1] == u] for u in users}
    for u, cs in by_user.items():
        assert cs, "each user must have performed calls"
        for c in cs:
            if c[2] == "send_email":
                assert c[3]["to"] == [f"{u}@x.test"]
    assert {c[1] for c in env.calls} == set(users)


async def test_high_risk_transfer_pauses_for_officer_then_resumes(env):
    uid = str(uuid.uuid4())
    c = cfg(uid)
    await env.graph.ainvoke(first_input(uid, "transfer 50000 to a new payee"), c)
    snap = await env.graph.aget_state(c)
    assert snap.next == ("await_approval",)
    assert snap.tasks[0].interrupts[0].value["type"] == "approval"
    assert not [x for x in env.calls if x[2] in ("send_email", "create_event")]  # nothing executed while pending
    out = await env.graph.ainvoke(Command(resume={"decision": "approved", "note": "ok"}), c)
    assert out["approval"]["status"] == "approved"
    assert any(x[2] == "send_email" for x in env.calls)  # approved -> confirmation email


async def test_rejected_transfer_skips_specialists_and_notifications(env):
    uid = str(uuid.uuid4())
    c = cfg(uid)
    await env.graph.ainvoke(first_input(uid, "transfer 50000"), c)
    out = await env.graph.ainvoke(Command(resume={"decision": "rejected", "note": "no"}), c)
    assert out["approval"]["status"] == "rejected"
    assert "faq_agent" not in out["evidence"] and not [x for x in env.calls if x[2] == "send_email"]


async def test_clarification_interrupt_resumes_with_customer_answer(env, monkeypatch):
    uid = str(uuid.uuid4())
    c = cfg(uid)
    asked = {"n": 0}
    orig = FakeStructured.ainvoke

    async def clar(self, msgs):
        if self.schema is Understanding and asked["n"] == 0:
            asked["n"] += 1
            return Understanding(intents=["fund_transfer"], needs_clarification=True, clarification_question="How much?")
        return await orig(self, msgs)

    monkeypatch.setattr(FakeStructured, "ainvoke", clar)
    await env.graph.ainvoke(first_input(uid, "send money"), c)
    snap = await env.graph.aget_state(c)
    assert snap.tasks[0].interrupts[0].value == {"type": "clarification", "question": "How much?"}
    out = await env.graph.ainvoke(Command(resume="5 home loan rate"), c)
    assert out["clarifications"] == 1 and out["answer"]
