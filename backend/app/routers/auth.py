import re
import uuid
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..agents.helpers import redis_client
from ..config import settings
from ..db import get_session
from ..deps import current_user
from ..models import User
from ..security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])
CHANNEL_KEYS = {"loans", "compliance", "audit", "rm"}


class Register(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=128)


class Login(BaseModel):
    email: EmailStr
    password: str


class Profile(BaseModel):
    name: str | None = Field(None, max_length=100)
    timezone: str | None = None
    prefs: dict[str, str] | None = None


def public(u: User) -> dict:
    return {"id": str(u.id), "email": u.email, "name": u.name, "role": u.role, "timezone": u.timezone, "prefs": u.prefs or {}}


async def _throttle(request: Request, key: str, limit: int = 10, window: int = 60) -> None:
    k = f"rl:auth:{request.client.host if request.client else 'x'}:{key}"
    r = redis_client()
    n = await r.incr(k)
    if n == 1:
        await r.expire(k, window)
    if n > limit:
        raise HTTPException(429, "Too many attempts, try again in a minute")


@router.post("/register")
async def register(body: Register, request: Request, session: AsyncSession = Depends(get_session)):
    await _throttle(request, "register")
    email = body.email.lower()
    if (await session.execute(select(User).where(User.email == email))).scalar_one_or_none():
        raise HTTPException(409, "An account with this email already exists")
    user = User(
        email=email, name=body.name.strip(), password_hash=await hash_password(body.password),
        role="officer" if email in settings.officer_email_set else "customer", timezone=settings.default_timezone,
    )
    session.add(user)
    await session.commit()
    return {"token": create_access_token(str(user.id), user.role), "user": public(user)}


@router.post("/login")
async def login(body: Login, request: Request, session: AsyncSession = Depends(get_session)):
    await _throttle(request, "login")
    user = (await session.execute(select(User).where(User.email == body.email.lower()))).scalar_one_or_none()
    ok = await verify_password(body.password, user.password_hash if user else "$2b$12$" + "x" * 53)
    if not user or not ok:
        raise HTTPException(401, "Invalid email or password")
    return {"token": create_access_token(str(user.id), user.role), "user": public(user)}


class Resume(BaseModel):
    ticket: str


@router.post("/resume")
async def resume(body: Resume, session: AsyncSession = Depends(get_session)):
    """Exchange the single-use ticket from the OAuth redirect for a normal session (Streamlit lost its state on the redirect)."""
    from ..oauth.flow import _redis
    from ..security import internal_claims

    try:
        claims = internal_claims(body.ticket, "frontend:resume")
    except Exception as exc:
        raise HTTPException(401, "This sign-in link has expired") from exc
    if not await _redis().set(f"resume:{claims['jti']}", "1", nx=True, ex=300):  # single use
        raise HTTPException(401, "This sign-in link was already used")
    user = await session.get(User, uuid.UUID(claims["sub"]))
    if not user:
        raise HTTPException(401, "Unknown user")
    return {"token": create_access_token(str(user.id), user.role), "user": public(user)}


@router.get("/me")
async def me(user: User = Depends(current_user)):
    return public(user)


@router.patch("/me")
async def update_me(body: Profile, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    if body.name:
        user.name = body.name.strip()
    if body.timezone:
        try:
            ZoneInfo(body.timezone)
        except Exception:
            raise HTTPException(422, "Unknown timezone") from None
        user.timezone = body.timezone
    if body.prefs is not None:
        clean = {}
        for k, v in body.prefs.items():
            if k not in CHANNEL_KEYS:
                raise HTTPException(422, f"Unknown channel setting '{k}'")
            if not re.fullmatch(r"#?[a-z0-9_\-]{1,80}", v.strip().lower()):
                raise HTTPException(422, f"Invalid Slack channel name for '{k}'")
            clean[k] = v.strip().lower()
        user.prefs = {**(user.prefs or {}), **clean}
    await session.commit()
    return public(user)
