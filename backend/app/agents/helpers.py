"""Small shared helpers for the agent nodes: the LLM factory, Redis client, trace steps and text utilities."""
import json
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import redis.asyncio as aioredis
from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.config import get_stream_writer

from ..config import settings


@lru_cache
def llm() -> BaseChatModel:
    """Chat model chosen by LLM_PROVIDER: 'ollama' (local, no API key) or 'anthropic'."""
    from ..audit import AuditCallback

    audit = [AuditCallback(settings.llm_model)]  # every prompt/response -> audit_events (llm_call)
    if settings.llm_provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=settings.llm_model, api_key=settings.anthropic_api_key, max_tokens=2048, max_retries=3, timeout=90, callbacks=audit)
    if settings.llm_provider == "openai":  # any OpenAI-compatible endpoint: Groq, OpenRouter, Together, vLLM ...
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=settings.llm_model, api_key=settings.openai_api_key, base_url=settings.openai_base_url, temperature=0,
                          max_tokens=2048, max_retries=3, timeout=90, callbacks=audit)
    from langchain_ollama import ChatOllama

    return ChatOllama(model=settings.llm_model, base_url=settings.ollama_base_url, temperature=0, num_predict=2048,
                      client_kwargs={"timeout": 300}, callbacks=audit)


@lru_cache
def redis_client() -> aioredis.Redis:
    """Shared async Redis client (locks, idempotency keys, streaming events)."""
    return aioredis.from_url(settings.redis_url, decode_responses=True)


def emit(**event) -> None:
    """Live agent-activity event for the Streamlit UI (custom stream channel)."""
    try:
        get_stream_writer()(event)
    except Exception:  # outside a graph run (tests)
        pass


def step(agent: str, detail: str, status: str = "done") -> dict:
    """Emit a UI event and return a trace entry to persist with the message."""
    emit(type="step", agent=agent, status=status, detail=detail)
    return {"agent": agent, "status": status, "detail": detail, "ts": datetime.now(timezone.utc).isoformat()}


def text_of(content) -> str:
    """Plain text of an LLM message whose content may be a string or a list of content blocks."""
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def resolve_relative_date(text: str, now: datetime) -> str | None:
    """Deterministic 'today' / 'tomorrow' / '[this|next] <weekday>' -> YYYY-MM-DD (LLMs get this wrong).
    'Thursday' and 'this Thursday' = the next such day, today included; 'next Thursday' = strictly after today."""
    t = text.lower()
    today = now.date()
    if re.search(r"\bday after tomorrow\b", t):
        return (today + timedelta(days=2)).isoformat()
    if re.search(r"\btomorrow\b", t):
        return (today + timedelta(days=1)).isoformat()
    if re.search(r"\btoday\b", t):
        return today.isoformat()
    m = re.search(r"\b(?:(next|this|coming)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", t)
    if not m:
        return None
    ahead = (_WEEKDAYS.index(m.group(2)) - today.weekday()) % 7
    if m.group(1) == "next" and ahead == 0:
        ahead = 7
    return (today + timedelta(days=ahead)).isoformat()


def last_user_text(state) -> str:
    """The customer's most recent message in this conversation."""
    for m in reversed(state.get("messages", [])):
        if m.type == "human":
            return text_of(m.content)
    return ""


async def once(key: str, ttl: int = 86400) -> bool:
    """True the first time `key` is seen -> side effects are not duplicated on retries."""
    return bool(await redis_client().set(f"once:{key}", "1", nx=True, ex=ttl))


def slack_channel(prefs: dict | None, which: str) -> str:
    """Slack channel for a purpose (loans, compliance, audit, rm): the user's preference or the default,
    sanitised to a plain #channel name.
    """
    defaults = {"loans": "#loans-team", "compliance": "#compliance-alerts", "audit": "#audit-log", "rm": "#rm-notifications"}
    raw = (prefs or {}).get(which) or defaults[which]
    raw = re.sub(r"[^\w#\-]", "", raw)
    return raw if raw.startswith("#") else f"#{raw}"


_PHONE = re.compile(r"(?<![\w.,])\+?\(?\d[\d\s\-().]{6,}\d(?![\w,]|\.\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"(?:https?://|www\.)[^\s)>\]]+", re.I)


def ungrounded_contacts(text: str, known: str) -> list[str]:
    """Phone numbers, email addresses and links in `text` that do not appear in `known` (the facts and the
    conversation). A language model can make these up; a wrong hotline number is harmful in a banking reply."""
    known_low, known_digits = known.lower(), re.sub(r"\D", "", known)
    found = []
    for m in _PHONE.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if len(digits) >= 9 and digits not in known_digits:
            found.append(m.group().strip())
    for rx in (_EMAIL, _URL):
        found += [m.group().rstrip(".,;") for m in rx.finditer(text) if m.group().rstrip(".,;").lower() not in known_low]
    return found


def strip_ungrounded_contacts(text: str, known: str) -> tuple[str, list[str]]:
    """Remove every sentence that contains an invented phone number, email address or link.
    Returns (clean_text, removed_items). Lines left empty (e.g. a bare bullet) are dropped."""
    bad = ungrounded_contacts(text, known)
    if not bad:
        return text, []
    out = []
    for line in text.split("\n"):
        if not any(b in line for b in bad):
            out.append(line)
            continue
        lead = re.match(r"\s*(?:[-*\u2022]|\d+[.)])?\s*", line).group()
        kept = [s for s in re.split(r"(?<=[.!?])\s+", line[len(lead):]) if not any(b in s for b in bad)]
        if kept:
            out.append(lead + " ".join(kept))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip(), bad


def pretty(obj) -> str:
    """Compact JSON for prompts, capped so a large payload cannot crowd out the rest of the context."""
    return json.dumps(obj, ensure_ascii=False, default=str, indent=1)[:6000]
