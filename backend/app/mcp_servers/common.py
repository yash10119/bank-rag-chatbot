"""Shared helpers for the MCP servers.

Identity model: the API signs a 60-second JWT (sub=user_id, aud=mcp:<service>)
for every tool call. The server verifies it and loads *that user's* encrypted
OAuth token. No tool accepts a user id or token as an argument, so a caller
cannot make a tool act on behalf of anyone but the verified subject.
"""
import re

import httpx
from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError

from ..oauth.tokens import NotConnected, get_access_token
from ..security import verify_internal_token

EMAIL_RE = re.compile(r"^[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[^@\s<>\"',;]+$")


async def authed(ctx: Context, service: str) -> tuple[str, str]:
    """Authenticate one tool call. The caller's identity comes only from the signed internal token in the
    Authorization header (never from tool arguments); that user's own access token is then loaded and refreshed
    if needed. Returns (user_id, access_token) or raises a ToolError the agent can report.
    """
    request = ctx.request_context.request
    header = request.headers.get("authorization", "") if request is not None else ""
    if not header.lower().startswith("bearer "):
        raise ToolError("unauthorized: missing identity token")
    try:
        user_id = verify_internal_token(header[7:], service)
    except Exception as exc:
        raise ToolError("unauthorized: invalid identity token") from exc
    try:
        token = await get_access_token(user_id, service)
    except NotConnected as exc:
        raise ToolError(f"not_connected:{service}") from exc
    return user_id, token


def register_oauth(mcp, service: str) -> None:
    """Host this service's browser OAuth endpoints on its MCP server."""
    from starlette.requests import Request
    from starlette.responses import RedirectResponse

    from ..oauth import flow, providers

    @mcp.custom_route("/oauth/start", methods=["GET"])
    async def oauth_start(request: Request):
        """GET /oauth/start: verify the connect ticket and redirect the browser to the provider's consent page."""
        try:
            return RedirectResponse(await flow.begin(service, request.query_params.get("ticket", "")), status_code=302)
        except providers.OAuthError as exc:
            return RedirectResponse(flow.back_to_app(service, "Not configured", str(exc), False), status_code=302)
        except Exception:  # forged / expired / missing ticket
            return RedirectResponse(flow.back_to_app(service, "Link expired", "Start the connection again from the app.", False), status_code=302)

    @mcp.custom_route("/oauth/callback", methods=["GET"])
    async def oauth_callback(request: Request):
        """GET /oauth/callback: validate state, exchange the code server-side, store the encrypted token and send
        the browser back to the Streamlit app. Tokens never reach the browser.
        """
        q = request.query_params
        title, message, ok, uid = await flow.finish(service, q.get("code"), q.get("state"), q.get("error"))
        return RedirectResponse(flow.back_to_app(service, title, message, ok, uid), status_code=302)  # back to the Streamlit app


def bearer(token: str) -> dict[str, str]:
    """Authorization header for a provider API call."""
    return {"Authorization": f"Bearer {token}"}


def check_emails(addresses: list[str]) -> list[str]:
    """Validate recipient addresses before any send, so a malformed or injected address fails loudly."""
    cleaned = [a.strip() for a in addresses if a and a.strip()]
    for a in cleaned:
        if not EMAIL_RE.match(a):
            raise ToolError(f"invalid email address: {a!r}")
    return cleaned


def raise_for_google(resp: httpx.Response) -> None:
    """Turn a Google API error into a ToolError. 401/403 are reported as not_connected so the UI asks the user
    to reconnect instead of showing a raw API error.
    """
    if resp.status_code >= 400:
        try:
            msg = resp.json().get("error", {}).get("message", resp.text)
        except Exception:
            msg = resp.text
        if resp.status_code in (401, 403):
            raise ToolError(f"not_connected:permission ({msg})")
        raise ToolError(f"google_api_error {resp.status_code}: {msg}")
