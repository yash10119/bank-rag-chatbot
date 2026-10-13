"""OAuth2 authorization-code flows for Google (Gmail, Calendar) and Slack."""
import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

import httpx

from ..config import settings
from . import clients

SERVICES: dict[str, dict] = {
    "gmail": {
        "provider": "google",
        "label": "Gmail",
        "scopes": [
            "openid",
            "email",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.readonly",
        ],
    },
    "calendar": {
        "provider": "google",
        "label": "Google Calendar",
        "scopes": [
            "openid",
            "email",
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/calendar.freebusy",
        ],
    },
    "slack": {
        "provider": "slack",
        "label": "Slack",
        "scopes": ["chat:write", "chat:write.public", "channels:read", "users:read", "users:read.email"],
    },
}

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"
SLACK_AUTH = "https://slack.com/oauth/v2/authorize"
SLACK_TOKEN = "https://slack.com/api/oauth.v2.access"
GOOGLE_REVOKE = "https://oauth2.googleapis.com/revoke"
SLACK_REVOKE = "https://slack.com/api/auth.revoke"


class OAuthError(Exception):
    """A provider-side or configuration failure in an OAuth step, with a message safe to show the user."""
    pass


def redirect_uri(service: str, cfg: dict | None = None) -> str:
    """Where the provider sends the browser back: the MCP server that owns this service."""
    if SERVICES[service]["provider"] == "slack":
        return (cfg or {}).get("redirect_uri") or settings.slack_redirect_uri
    base = settings.mcp_public_gmail if service == "gmail" else settings.mcp_public_calendar
    return f"{base.rstrip('/')}/oauth/callback"


def new_pkce() -> tuple[str, str]:
    """PKCE verifier and its S256 challenge. The verifier stays server-side with the state, so an intercepted
    authorization code cannot be exchanged by anyone else.
    """
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def client_for(service: str) -> dict:
    """The app's OAuth client for this service (Settings page first, .env fallback). Raises if none is set."""
    cfg = await clients.get(SERVICES[service]["provider"])
    if not cfg:
        raise OAuthError(f"{SERVICES[service]['label']} is not set up yet: a bank officer must add the OAuth client in Settings > Integrations setup")
    return cfg


async def is_configured(service: str) -> bool:
    """Whether an OAuth client exists for this service's provider (Settings page or .env)."""
    return await clients.get(SERVICES[service]["provider"]) is not None


async def build_authorization_url(service: str, state: str, code_challenge: str) -> str:
    """The provider consent URL for a service: minimal scopes, the single-use state and, for Google, PKCE plus
    offline access with forced consent so a refresh token is always issued.
    """
    spec = SERVICES[service]
    cfg = await client_for(service)
    if spec["provider"] == "google":
        params = {
            "client_id": cfg["client_id"],
            "redirect_uri": redirect_uri(service),
            "response_type": "code",
            "scope": " ".join(spec["scopes"]),
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{GOOGLE_AUTH}?{urlencode(params)}"
    params = {
        "client_id": cfg["client_id"],
        "scope": ",".join(spec["scopes"]),
        "redirect_uri": redirect_uri(service, cfg),
        "state": state,
    }
    return f"{SLACK_AUTH}?{urlencode(params)}"


async def exchange_code(service: str, code: str, code_verifier: str) -> tuple[dict, str, str]:
    """Returns (token_payload, granted_scopes, account_label)."""
    spec = SERVICES[service]
    cfg = await client_for(service)
    async with httpx.AsyncClient(timeout=20) as client:
        if spec["provider"] == "google":
            r = await client.post(
                GOOGLE_TOKEN,
                data={
                    "code": code,
                    "client_id": cfg["client_id"],
                    "client_secret": cfg["client_secret"],
                    "redirect_uri": redirect_uri(service),
                    "grant_type": "authorization_code",
                    "code_verifier": code_verifier,
                },
            )
            data = r.json()
            if r.status_code != 200 or "access_token" not in data:
                raise OAuthError(data.get("error_description") or data.get("error") or "Google token exchange failed")
            if not data.get("refresh_token"):
                raise OAuthError("Google did not return a refresh token; revoke app access and retry")
            ui = await client.get(GOOGLE_USERINFO, headers={"Authorization": f"Bearer {data['access_token']}"})
            label = ui.json().get("email", "") if ui.status_code == 200 else ""
            payload = {
                "access_token": data["access_token"],
                "refresh_token": data["refresh_token"],
                "expires_at": int(time.time()) + int(data.get("expires_in", 3600)),
            }
            return payload, data.get("scope", ""), label

        r = await client.post(
            SLACK_TOKEN,
            data={
                "code": code,
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "redirect_uri": redirect_uri(service, cfg),
            },
        )
        data = r.json()
        if not data.get("ok"):
            raise OAuthError(f"Slack: {data.get('error', 'token exchange failed')}")
        payload = {
            "access_token": data["access_token"],
            "team_id": data.get("team", {}).get("id"),
            "bot_user_id": data.get("bot_user_id"),
        }
        return payload, data.get("scope", ""), data.get("team", {}).get("name", "")


def missing_scopes(service: str, granted: str) -> list[str]:
    """Scopes the service needs that the user did not grant (Google lets users untick
    individual permissions on the consent screen; Slack lists them comma-separated)."""
    have = {s for s in granted.replace(",", " ").split() if s}
    return [s for s in SERVICES[service]["scopes"] if s not in have and s not in ("openid", "email")]


async def revoke(service: str, payload: dict) -> None:
    """Best-effort revocation at the provider so a disconnected token stops working everywhere."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if SERVICES[service]["provider"] == "google":
                token = payload.get("refresh_token") or payload.get("access_token", "")
                await client.post(GOOGLE_REVOKE, data={"token": token})
            else:
                await client.post(SLACK_REVOKE, headers={"Authorization": f"Bearer {payload.get('access_token', '')}"})
    except httpx.HTTPError:
        pass  # local deletion still happens; the user can revoke from their provider account page


async def refresh_google(refresh_token: str) -> dict:
    """Exchange a refresh token for a new access token. Slack bot tokens do not expire, so only Google needs this."""
    cfg = await clients.get("google")
    if not cfg:
        raise OAuthError("Google OAuth client was removed; reconnect after it is configured again")
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            GOOGLE_TOKEN,
            data={
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    data = r.json()
    if r.status_code != 200 or "access_token" not in data:
        raise OAuthError(data.get("error_description") or data.get("error") or "refresh failed")
    return {"access_token": data["access_token"], "expires_at": int(time.time()) + int(data.get("expires_in", 3600))}
