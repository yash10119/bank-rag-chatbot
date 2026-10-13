"""OAuth *client* (app) credentials, configured at runtime instead of hard-coded.

A bank officer pastes the Google / Slack client ID and secret into the Settings page. They are stored in
`oauth_clients` as Fernet ciphertext (key derived by HKDF for this purpose only), never returned to the browser,
and can be replaced or removed without a restart. `.env` values are only a fallback for automated deployments.

The client identifies the *application* to Google/Slack; it is not a user credential. Every user still signs in
with their own account and gets their own token (see tokens.py).
"""
import time

from sqlalchemy import delete, select

from ..config import settings
from ..db import SessionLocal
from ..models import OAuthClient
from ..security import decrypt_token, encrypt_token

PROVIDERS = ("google", "slack")
_KEY_OWNER = "app-config"  # HKDF context: client secrets use a different derived key than any user's tokens
_TTL = 15  # seconds; API and the three MCP servers are separate processes, so a short cache lets UI changes propagate
_cache: dict[str, tuple[float, dict | None]] = {}


def _env(provider: str) -> dict | None:
    if provider == "google" and settings.google_client_id and settings.google_client_secret:
        return {"client_id": settings.google_client_id, "client_secret": settings.google_client_secret, "source": "env"}
    if provider == "slack" and settings.slack_client_id and settings.slack_client_secret:
        return {"client_id": settings.slack_client_id, "client_secret": settings.slack_client_secret,
                "redirect_uri": settings.slack_redirect_uri, "source": "env"}
    return None


async def _load(provider: str) -> dict | None:
    """Decrypted row from the database, or None. Never raises (a DB hiccup must not look like 'configured')."""
    try:
        async with SessionLocal() as s:
            row = (await s.execute(select(OAuthClient).where(OAuthClient.provider == provider))).scalar_one_or_none()
        if row is None:
            return None
        return {**decrypt_token(_KEY_OWNER, f"client:{provider}", row.enc_blob), "source": "database", "updated_at": row.updated_at.isoformat()}
    except Exception:
        return None


async def get(provider: str) -> dict | None:
    """Client config for a provider: the Settings-page value wins, `.env` is the fallback, None = not configured."""
    now = time.monotonic()
    hit = _cache.get(provider)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    cfg = await _load(provider) or _env(provider)
    if cfg and provider == "slack" and not cfg.get("redirect_uri"):
        cfg = None  # Slack cannot start without its registered HTTPS redirect
    _cache[provider] = (now, cfg)
    return cfg


async def save(provider: str, client_id: str, client_secret: str, redirect_uri: str | None, by) -> None:
    """Encrypt and store the application's OAuth client for a provider, replacing any previous one."""
    payload = {"client_id": client_id.strip(), "client_secret": client_secret.strip()}
    if redirect_uri:
        payload["redirect_uri"] = redirect_uri.strip()
    blob = encrypt_token(_KEY_OWNER, f"client:{provider}", payload)
    async with SessionLocal() as s:
        row = (await s.execute(select(OAuthClient).where(OAuthClient.provider == provider))).scalar_one_or_none()
        if row:
            row.enc_blob, row.updated_by = blob, by
        else:
            s.add(OAuthClient(provider=provider, enc_blob=blob, updated_by=by))
        await s.commit()
    _cache.pop(provider, None)


async def remove(provider: str) -> None:
    """Delete the stored OAuth client for a provider and drop it from the cache."""
    async with SessionLocal() as s:
        await s.execute(delete(OAuthClient).where(OAuthClient.provider == provider))
        await s.commit()
    _cache.pop(provider, None)


def mask(client_id: str) -> str:
    """Enough of the client ID to recognise it; the secret is never shown at all."""
    return client_id if len(client_id) <= 14 else f"{client_id[:8]}…{client_id[-10:]}"
