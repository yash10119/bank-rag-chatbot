"""Typed state of the banking graph, checkpointed per conversation."""
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

RESET = {"__reset__": True}


def merge_dict(old: dict | None, new: dict | None) -> dict:
    """Parallel specialists write disjoint keys. {"__reset__": True, ...} starts a fresh turn."""
    new = new or {}
    if new.get("__reset__"):
        return {k: v for k, v in new.items() if k != "__reset__"}
    return {**(old or {}), **new}


def reduce_list(old: list | None, new: list | None) -> list:
    """List reducer for parallel branches: appends, and a leading RESET marker replaces the list (used to
    start each turn with an empty trace/actions list).
    """
    new = new or []
    if new and isinstance(new[0], dict) and new[0].get("__reset__"):
        return new[1:]
    return (old or []) + new


class BankState(TypedDict, total=False):
    """Everything one conversation turn carries between nodes. Identity fields come from the authenticated
    session; only findings, entities and messages are ever produced by the model.
    """
    # identity: set by the API from the authenticated session, never from model output
    user_id: str
    user_name: str
    user_email: str
    timezone: str
    conversation_id: str
    turn_id: str

    messages: Annotated[list[AnyMessage], add_messages]
    connections: dict[str, bool]
    profile: dict | None
    prefs: dict

    # understanding / routing
    intents: list[str]
    entities: dict[str, Any]
    clarifications: int
    risk: dict
    approval: dict | None

    # per-turn working memory (reset by intake)
    evidence: Annotated[dict, merge_dict]
    actions: Annotated[list[dict], reduce_list]
    trace: Annotated[list[dict], reduce_list]
    appointment: dict | None
    answer: str
