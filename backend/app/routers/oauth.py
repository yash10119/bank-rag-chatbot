"""OAuth connect endpoints used by Streamlit. The consent flow itself (redirect, callback,
token storage) runs on the MCP servers - see app/oauth/flow.py. The API only authenticates
the user and hands back a short-lived connect ticket URL pointing at the right MCP server."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import log_event
from ..config import settings
from ..db import get_session
from ..deps import current_user, officer
from ..models import User
from ..oauth import clients, flow, providers
from ..oauth.tokens import connection_status, delete_connection

router = APIRouter(prefix="/oauth", tags=["oauth"])


@router.get("/services")
async def services(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    status = await connection_status(session, user.id)
    return {
        name: {"label": spec["label"], "configured": await providers.is_configured(name), "simulated": settings.mcp_mock,
               **status.get(name, {"connected": False, "account": ""})}
        for name, spec in providers.SERVICES.items()
    }


@router.post("/{service}/start")
async def start(service: str, user: User = Depends(current_user)):
    if service not in providers.SERVICES:
        raise HTTPException(404, "Unknown service")
    if settings.mcp_mock:
        raise HTTPException(409, "Mock mode is on (MCP_MOCK=true): no connection is needed.")
    if not await providers.is_configured(service):
        raise HTTPException(503, f"{providers.SERVICES[service]['label']} is not set up yet. A bank officer must add the OAuth client under Integrations setup.")
    return {"url": flow.connect_url(service, str(user.id))}


# ------------------------------------------------------------ app OAuth clients (officers only)


class ClientIn(BaseModel):
    client_id: str = Field(min_length=8, max_length=300)
    client_secret: str = Field(min_length=8, max_length=300)
    redirect_uri: str | None = Field(None, max_length=500)


def _redirects(provider: str, cfg: dict | None) -> list[str]:
    """Exactly what must be registered in the provider console for the flow to work."""
    if provider == "google":
        return [providers.redirect_uri("gmail"), providers.redirect_uri("calendar")]
    uri = (cfg or {}).get("redirect_uri") or settings.slack_redirect_uri
    return [uri] if uri and "your-tunnel" not in uri else []


@router.get("/clients")
async def list_clients(user: User = Depends(officer)):
    """Which OAuth clients are configured. Never returns a secret; the client ID is masked."""
    out = {}
    for provider in clients.PROVIDERS:
        cfg = await clients.get(provider)
        out[provider] = {
            "configured": cfg is not None, "source": (cfg or {}).get("source"), "client_id": clients.mask(cfg["client_id"]) if cfg else None,
            "updated_at": (cfg or {}).get("updated_at"), "redirect_uris": _redirects(provider, cfg),
            "services": [n for n, s in providers.SERVICES.items() if s["provider"] == provider],
            "scopes": sorted({sc for s in providers.SERVICES.values() if s["provider"] == provider for sc in s["scopes"]}),
        }
    return out


@router.put("/clients/{provider}")
async def save_client(provider: str, body: ClientIn, user: User = Depends(officer)):
    if provider not in clients.PROVIDERS:
        raise HTTPException(404, "Unknown provider")
    cid = body.client_id.strip()
    if provider == "google" and not cid.endswith(".apps.googleusercontent.com"):
        raise HTTPException(422, "That does not look like a Google OAuth client ID (it ends with .apps.googleusercontent.com)")
    if provider == "slack":
        uri = (body.redirect_uri or "").strip()
        if not uri.startswith("https://") or not uri.endswith("/oauth/callback"):
            raise HTTPException(422, "Slack needs an https redirect URL ending in /oauth/callback (your tunnel or domain for the Slack MCP server)")
    await clients.save(provider, cid, body.client_secret, body.redirect_uri if provider == "slack" else None, user.id)
    await log_event("oauth_client_saved", user_id=str(user.id), provider=provider, client_id=clients.mask(cid))  # never the secret
    return {"ok": True}


@router.delete("/clients/{provider}")
async def remove_client(provider: str, user: User = Depends(officer)):
    if provider not in clients.PROVIDERS:
        raise HTTPException(404, "Unknown provider")
    await clients.remove(provider)
    await log_event("oauth_client_removed", user_id=str(user.id), provider=provider)
    return {"ok": True}


@router.delete("/{service}")
async def disconnect(service: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    if service not in providers.SERVICES:
        raise HTTPException(404, "Unknown service")
    await delete_connection(session, user.id, service)
    return {"ok": True}
