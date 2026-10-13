"""The banking reasoning graph.

        START
          |
        intake ----(ambiguous)----> clarify --(customer answers)--> intake      [HITL: customer]
          |
       risk_gate --(out of scope)--> respond
          |--(high risk)--> request_approval -> await_approval                    [HITL: officer]
          |                                        |--(rejected)--> respond
          v                                        v (approved)
       dispatch  ==Send fan-out==>  rates_agent | eligibility_agent | transaction_agent | analytics_agent | inbox_agent | faq_agent
                                                   \\        |         |           /
                                                    v       v         v          v
                                                         scheduler (calendar, no double-booking)
                                                              |
                                                           notify (Gmail / Slack, in parallel)
                                                              |
                                                           respond (streamed) -> audit -> END
"""
from langgraph.graph import END, START, StateGraph

from .actions import notify, scheduler
from .core import (
    audit,
    await_approval,
    clarify,
    intake,
    request_approval,
    respond,
    risk_gate,
    route_after_approval,
    route_after_intake,
)
from .specialists import analytics_agent, dispatch, eligibility_agent, faq_agent, inbox_agent, rates_agent, transaction_agent
from .state import BankState

SPECIALISTS = [rates_agent, eligibility_agent, transaction_agent, analytics_agent, inbox_agent, faq_agent]


def route_after_risk(state):
    """Conditional edge after the risk gate: decline out-of-scope requests, pause high-risk ones for approval,
    otherwise fan out to the specialists the intents need.
    """
    if set(state.get("intents", [])) <= {"out_of_scope"}:
        return "respond"
    if state["risk"]["level"] == "high":
        return "request_approval"
    return dispatch(state)


def route_after_approval_decision(state):
    """Conditional edge after an officer decides: fan out on approval, otherwise go straight to the reply."""
    return dispatch(state) if route_after_approval(state) == "dispatch" else "respond"


def build_graph(checkpointer):
    """Assemble and compile the graph shown in the module docstring. Specialists are reached through Send
    fan-out and all rejoin at the scheduler, so side effects run once after every finding is in.
    """
    g = StateGraph(BankState)
    for fn in (intake, clarify, risk_gate, request_approval, await_approval, scheduler, notify, respond, audit, *SPECIALISTS):
        g.add_node(fn.__name__, fn)

    g.add_edge(START, "intake")
    g.add_conditional_edges("intake", route_after_intake, ["clarify", "risk_gate"])
    g.add_edge("clarify", "intake")
    names = [fn.__name__ for fn in SPECIALISTS]
    g.add_conditional_edges("risk_gate", route_after_risk, ["respond", "request_approval", "scheduler", *names])
    g.add_edge("request_approval", "await_approval")
    g.add_conditional_edges("await_approval", route_after_approval_decision, ["respond", "scheduler", *names])
    for spec in SPECIALISTS:
        g.add_edge(spec.__name__, "scheduler")
    g.add_edge("scheduler", "notify")
    g.add_edge("notify", "respond")
    g.add_edge("respond", "audit")
    g.add_edge("audit", END)
    return g.compile(checkpointer=checkpointer)
