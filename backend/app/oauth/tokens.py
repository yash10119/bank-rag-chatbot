"""Encrypted, per-user OAuth token storage. Every lookup is keyed by user_id and
the ciphertext can only be opened with a key derived from that same user_id."""
import time
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import log_event
from ..config import settings
from ..db import SessionLocal
from ..models import OAuthConnection
from ..security import decrypt_token, encrypt_token
from .providers import SERVICES, OAuthError, refresh_google, revoke


class NotConnected(Exception):
    def __init__(self, service: str):
        super().__init__(f"not_connected:{service}")
        self.service = service


async def save_connection(
    session: AsyncSession, user_id: uuid.UUID, service: str, payload: dict, scopes: str, label: str
) -> None:
    blob = encrypt_token(str(user_id), service, payload)
    row = (
        await session.execute(
            select(OAuthConnection).where(OAuthConnection.user_id == user_id, OAuthConnection.service == service)
        )
    ).scalar_one_or_none()
    if row:
        row.enc_blob, row.scopes, row.account_label = blob, scopes, label
    else:
        session.add(OAuthConnection(user_id=user_id, service=service, enc_blob=blob, scopes=scopes, account_label=label))
    await session.commit()
    await _flag_expired(user_id, service, False)


async def delete_connection(session: AsyncSession, user_id: uuid.UUID, service: str) -> None:
    row = (
        await session.execute(
            select(OAuthConnection).where(OAuthConnection.user_id == user_id, OAuthConnection.service == service)
        )
    ).scalar_one_or_none()
    if row and not settings.mcp_mock:
        try:
            await revoke(service, decrypt_token(str(user_id), service, row.enc_blob))
        except ValueError:
            pass  # undecryptable blob: just delete it
    await session.execute(
        delete(OAuthConnection).where(OAuthConnection.user_id == user_id, OAuthConnection.service == service)
    )
    await session.commit()
    await log_event("oauth_disconnected", user_id=user_id, service=service, revoked_at_provider=bool(row) and not settings.mcp_mock)


def _expired_key(user_id, service: str) -> str:
    return f"oauth:expired:{user_id}:{service}"


async def _flag_expired(user_id, service: str, on: bool) -> None:
    from .flow import _redis  # local import: flow imports this module

    try:
        r = _redis()
        await (r.set(_expired_key(user_id, service), "1", ex=30 * 86400) if on else r.delete(_expired_key(user_id, service)))
    except Exception:  # status flag is best-effort
        pass


async def connection_status(session: AsyncSession, user_id: uuid.UUID) -> dict[str, dict]:
    if settings.mcp_mock:
        return {s: {"connected": True, "account": "Local mock (no OAuth)"} for s in SERVICES}
    rows = (await session.execute(select(OAuthConnection).where(OAuthConnection.user_id == user_id))).scalars()
    out: dict[str, dict] = {}
    from .flow import _redis

    for r in rows:
        try:
            expired = bool(await _redis().exists(_expired_key(user_id, r.service)))
        except Exception:
            expired = False
        out[r.service] = {"connected": not expired, "expired": expired, "account": r.account_label}
    return out


async def get_access_token(user_id: str | uuid.UUID, service: str) -> str:
    """Return a valid access token for exactly this user, refreshing Google
    tokens when needed. Raises NotConnected if the user never connected."""
    if service not in SERVICES:
        raise NotConnected(service)
    if settings.mcp_mock:
        return "mock-token"
    uid = uuid.UUID(str(user_id))
    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(OAuthConnection)
                .where(OAuthConnection.user_id == uid, OAuthConnection.service == service)
                .with_for_update()  # serialise concurrent refreshes for this user+service
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotConnected(service)
        payload = decrypt_token(str(uid), service, row.enc_blob)
        if SERVICES[service]["provider"] == "google" and payload.get("expires_at", 0) - 60 < time.time():
            try:
                payload.update(await refresh_google(payload["refresh_token"]))
            except OAuthError as exc:
                await _flag_expired(uid, service, True)  # surfaces "token expired - reconnect" in the UI
                await log_event("oauth_refresh_failed", user_id=uid, service=service, reason=str(exc)[:200])
                raise NotConnected(service) from exc  # revoked -> user must reconnect
            row.enc_blob = encrypt_token(str(uid), service, payload)
            await session.commit()
            await log_event("oauth_refreshed", user_id=uid, service=service)
        return payload["access_token"]
