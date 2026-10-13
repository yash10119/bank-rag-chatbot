"""Read-only view of what the mock MCP servers captured for the calling user (MCP_MOCK=true)."""

from fastapi import APIRouter, Depends
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_session
from ..deps import current_user
from ..models import MockItem, User

router = APIRouter(prefix="/mock", tags=["mock"])


@router.get("/outbox")
async def outbox(service: str | None = None, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """Development only (MCP_MOCK=true): what the simulated Gmail, Calendar and Slack servers recorded for the caller."""
    if not settings.mcp_mock:
        return {"enabled": False, "items": []}
    q = select(MockItem).where(MockItem.user_id == user.id)
    if service:
        q = q.where(MockItem.service == service)
    rows = (await session.execute(q.order_by(MockItem.created_at.desc()).limit(200))).scalars()
    return {
        "enabled": True,
        "items": [{"id": str(r.id), "service": r.service, "kind": r.kind, "payload": r.payload, "at": r.created_at.isoformat()} for r in rows],
    }


@router.delete("/outbox")
async def clear(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """Development only: clear the caller's simulated outbox."""
    await session.execute(delete(MockItem).where(MockItem.user_id == user.id))
    await session.commit()
    return {"ok": True}
