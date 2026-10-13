"""FastAPI dependencies that authenticate the caller. Every data route depends on current_user,
so each query is scoped to the user in the session JWT and never to a client-supplied id."""
import uuid

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models import User
from .security import decode_access_token

_bearer = HTTPBearer(auto_error=False)


async def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer), session: AsyncSession = Depends(get_session)
) -> User:
    """Resolve the bearer JWT to a User row. Any failure gives the same 401 so callers cannot probe for accounts."""
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    if creds is None:
        raise unauthorized
    try:
        claims = decode_access_token(creds.credentials)
        user = await session.get(User, uuid.UUID(claims["sub"]))
    except (jwt.PyJWTError, ValueError, KeyError):
        raise unauthorized from None
    if user is None:
        raise unauthorized
    return user


async def officer(user: User = Depends(current_user)) -> User:
    """Require the bank-officer role (approvals, bank-wide files, monitoring, OAuth client setup)."""
    if user.role != "officer":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Bank officer role required")
    return user
