"""Slack MCP server (streamable HTTP). Posts with the calling user's own workspace token."""
import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from ..config import settings
from . import mock
from .common import EMAIL_RE, authed, bearer, register_oauth

API = "https://slack.com/api"
mcp = FastMCP("bank-slack", host="0.0.0.0", port=settings.mcp_port, stateless_http=True, json_response=True)
register_oauth(mcp, "slack")

SEVERITY_EMOJI = {"info": ":information_source:", "warning": ":warning:", "critical": ":rotating_light:"}


def esc(text: object) -> str:
    """Neutralise Slack control sequences (<!channel>, <@U123>, links) in untrusted text."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _call(client: httpx.AsyncClient, token: str, method: str, **payload) -> dict:
    r = await client.post(f"{API}/{method}", json=payload, headers=bearer(token))
    data = r.json()
    if not data.get("ok"):
        err = data.get("error", "unknown_error")
        if err in ("invalid_auth", "token_revoked", "account_inactive", "not_authed"):
            raise ToolError("not_connected:slack")
        raise ToolError(f"slack_error:{err}")
    return data


async def _post(client: httpx.AsyncClient, token: str, channel: str, text: str, blocks: list | None = None) -> dict:
    """chat.postMessage, falling back to the workspace's general channel when the target channel does not exist
    (a freshly connected workspace has no #loans-team etc.). The intended channel is named in the message."""
    extra = {"blocks": blocks} if blocks else {}
    try:
        data = await _call(client, token, "chat.postMessage", channel=channel, text=text, **extra)
        return {"ok": True, "channel": data.get("channel"), "ts": data.get("ts")}
    except ToolError as exc:
        if "channel_not_found" not in str(exc) or not channel.startswith("#"):
            raise
    r = await client.get(f"{API}/conversations.list", params={"types": "public_channel", "exclude_archived": "true", "limit": 200},
                         headers=bearer(token))
    general = next((c for c in r.json().get("channels", []) if c.get("is_general")), None)
    if not general:
        raise ToolError("slack_error:channel_not_found")
    note = f"(intended for {esc(channel)}, which does not exist in this workspace)"
    if blocks:
        extra = {"blocks": [*blocks, {"type": "context", "elements": [{"type": "mrkdwn", "text": note}]}]}
    data = await _call(client, token, "chat.postMessage", channel=general["id"], text=f"{text} {note}", **extra)
    return {"ok": True, "channel": data.get("channel"), "ts": data.get("ts"), "fallback_from": channel, "posted_to": f"#{general['name']}"}


@mcp.tool()
async def post_message(ctx: Context, channel: str, text: str) -> dict:
    """Post a plain message to a public channel (e.g. '#loans-team') or a user id."""
    uid, token = await authed(ctx, "slack")
    if settings.mcp_mock:
        return await mock.post(uid, "message", {"channel": channel, "text": esc(text)[:3000]})
    async with httpx.AsyncClient(timeout=15) as c:
        return await _post(c, token, channel, esc(text)[:3000])


@mcp.tool()
async def post_structured(
    ctx: Context, channel: str, title: str, fields: dict[str, str], severity: str = "info"
) -> dict:
    """Post a structured alert/summary card. severity: info | warning | critical."""
    uid, token = await authed(ctx, "slack")
    if settings.mcp_mock:
        return await mock.post(uid, "card", {"channel": channel, "title": esc(title)[:150], "severity": severity,
                                             "fields": {esc(k): esc(v)[:2000] for k, v in list(fields.items())[:10]}})
    emoji = SEVERITY_EMOJI.get(severity, SEVERITY_EMOJI["info"])
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{title}"[:150]}},
        {
            "type": "section",
            "fields": [{"type": "mrkdwn", "text": f"*{esc(k)}*\n{esc(v)}"[:2000]} for k, v in list(fields.items())[:10]],
        },
    ]
    async with httpx.AsyncClient(timeout=15) as c:
        return await _post(c, token, channel, f"{emoji} {esc(title)}", blocks)


@mcp.tool()
async def notify_user_by_email(ctx: Context, email: str, text: str) -> dict:
    """Direct-message a workspace member, looked up by email."""
    if not EMAIL_RE.match(email):
        raise ToolError("invalid email address")
    uid, token = await authed(ctx, "slack")
    if settings.mcp_mock:
        return await mock.post(uid, "dm", {"channel": f"DM to {email}", "to": email, "text": esc(text)[:3000]})
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{API}/users.lookupByEmail", params={"email": email}, headers=bearer(token))
        found = r.json()
        if not found.get("ok"):
            return {"ok": False, "error": f"user_not_found:{found.get('error', '')}"}
        data = await _call(c, token, "chat.postMessage", channel=found["user"]["id"], text=esc(text)[:3000])
    return {"ok": True, "channel": data.get("channel"), "ts": data.get("ts")}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
