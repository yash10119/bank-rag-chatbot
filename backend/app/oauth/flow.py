"""Browser OAuth flow, hosted by each MCP server (/oauth/start, /oauth/callback).

The API only mints a short-lived, single-purpose *connect ticket* for the logged-in user.
The MCP server verifies it, runs the provider consent flow (PKCE + single-use state bound
to that user and service) and stores the encrypted token under that user.
"""
import json
import secrets
import uuid
from functools import lru_cache
from urllib.parse import urlencode

import redis.asyncio as aioredis

from ..audit import log_event
from ..config import settings
from ..db import SessionLocal
from ..security import make_internal_token, verify_internal_token
from . import providers
from .tokens import save_connection

STATE_TTL = 600
TICKET_TTL = 300


@lru_cache
def _redis() -> aioredis.Redis:
    return aioredis.from_url(settings.redis_url, decode_responses=True)


def public_base(service: str) -> str:
    """Browser-reachable base URL of the MCP server that hosts this service's OAuth."""
    return {
        "gmail": settings.mcp_public_gmail,
        "calendar": settings.mcp_public_calendar,
        "slack": settings.mcp_public_slack,
    }[service].rstrip("/")


def connect_url(service: str, user_id: str) -> str:
    """Called by the API for an authenticated user."""
    ticket = make_internal_token(user_id, f"{service}:connect", ttl=TICKET_TTL)
    return f"{public_base(service)}/oauth/start?ticket={ticket}"


async def begin(service: str, ticket: str) -> str:
    """Verify the ticket, remember (user, service, PKCE verifier) under a single-use state, return the provider URL."""
    user_id = verify_internal_token(ticket, f"{service}:connect")  # raises on forged/expired
    await providers.client_for(service)  # raises OAuthError with a clear message if no client is configured
    state = secrets.token_urlsafe(32)
    verifier, challenge = providers.new_pkce()
    await _redis().set(f"oauth:state:{state}", json.dumps({"user_id": user_id, "service": service, "verifier": verifier}), ex=STATE_TTL)
    return await providers.build_authorization_url(service, state, challenge)


async def finish(service: str, code: str | None, state: str | None, error: str | None) -> tuple[str, str, bool, str | None]:
    """Returns (title, message, ok, user_id). user_id is None when the state was invalid (nobody to resume)."""
    if error:
        return "Connection cancelled", f"The provider returned: {error}", False, None
    if not code or not state:
        return "Invalid request", "Missing code or state.", False, None
    raw = await _redis().getdel(f"oauth:state:{state}")  # single use
    if not raw:
        return "Link expired", "Start the connection again from the app.", False, None
    data = json.loads(raw)
    uid = data["user_id"]
    if data["service"] != service:
        return "Invalid request", "Service mismatch.", False, None
    try:
        payload, scopes, label = await providers.exchange_code(service, code, data["verifier"])
    except providers.OAuthError as exc:
        await log_event("oauth_failed", user_id=uid, service=service, reason=str(exc)[:200])
        return "Connection failed", str(exc), False, uid
    if missing := providers.missing_scopes(service, scopes):
        await providers.revoke(service, payload)  # don't keep a half-granted token
        names = ", ".join(m.rsplit("/", 1)[-1] for m in missing)
        return "Permissions missing", f"Please tick every permission on the consent screen. Not granted: {names}.", False, uid
    async with SessionLocal() as session:
        await save_connection(session, uuid.UUID(data["user_id"]), service, payload, scopes, label)
    await log_event("oauth_connected", user_id=uid, service=service, account=label, scopes=scopes.split())
    return f"{providers.SERVICES[service]['label']} connected", f"Connected as {label or 'your account'}.", True, uid


RESUME_TTL = 120


def back_to_app(service: str, title: str, message: str, ok: bool, user_id: str | None = None) -> str:
    """URL of the Streamlit app carrying the outcome (shown as a toast; the status cards refresh).
    The redirect is a full page load, so Streamlit starts a fresh session: a 2-minute, single-use `resume`
    ticket (signed for this user, spent on first use via /auth/resume) restores the login without a password."""
    params = {"oauth": service, "status": "ok" if ok else "error", "msg": f"{title}: {message}"}
    if user_id:
        params["resume"] = make_internal_token(user_id, "frontend:resume", ttl=RESUME_TTL)
    return f"{settings.frontend_url.rstrip('/')}/?{urlencode(params)}"
