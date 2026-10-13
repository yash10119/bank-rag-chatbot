"""Async MCP client used by agent nodes. Each call carries a short-lived token that
names the *state's* user, never anything the LLM produced."""
import json
import time
from dataclasses import dataclass, field

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from .audit import log_event
from .config import settings
from .security import make_internal_token

URLS = {
    "gmail": lambda: settings.mcp_gmail_url,
    "calendar": lambda: settings.mcp_calendar_url,
    "slack": lambda: settings.mcp_slack_url,
}


@dataclass
class McpResult:
    ok: bool
    data: dict = field(default_factory=dict)
    error: str = ""

    @property
    def not_connected(self) -> bool:
        return "not_connected" in self.error


def _payload(result) -> dict:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and structured:
        return structured.get("result", structured) if set(structured) == {"result"} else structured
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            try:
                parsed = json.loads(text)
                return parsed if isinstance(parsed, dict) else {"result": parsed}
            except json.JSONDecodeError:
                return {"text": text}
    return {}


async def call_tool(service: str, user_id: str, tool: str, arguments: dict) -> McpResult:
    """Never raises for expected failures; returns McpResult(ok=False, error=...)."""
    token = make_internal_token(user_id, service)
    t0 = time.perf_counter()
    res = await _call(service, user_id, tool, arguments, token)
    # Audit row per MCP call: who, which tool, outcome, latency. Arguments are logged by key only (no email bodies/PII).
    await log_event("mcp_tool_call", user_id=user_id, service=service, tool=tool, ok=res.ok, error=res.error or None,
                    arg_keys=sorted(arguments), ms=int((time.perf_counter() - t0) * 1000))
    return res


async def _call(service: str, user_id: str, tool: str, arguments: dict, token: str) -> McpResult:
    try:
        async with streamablehttp_client(
            URLS[service](), headers={"Authorization": f"Bearer {token}"}, timeout=30
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments)
    except Exception as exc:  # transport / server down
        return McpResult(False, error=f"mcp_unreachable:{service}:{type(exc).__name__}")
    data = _payload(result)
    if result.isError:
        return McpResult(False, error=data.get("text", "tool_error"))
    if data.get("ok") is False:
        return McpResult(False, data=data, error=str(data.get("error", "failed")))
    return McpResult(True, data=data)
