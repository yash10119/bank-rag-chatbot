"""Passwords, session JWTs, per-user token encryption, internal MCP tokens."""
import asyncio
import base64
import json
import time
import uuid

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import settings

# ---------------------------------------------------------------- passwords


def _pw(password: str) -> bytes:
    return password.encode()[:72]  # bcrypt input limit


async def hash_password(password: str) -> str:
    """Bcrypt hash, computed in a worker thread because bcrypt is deliberately slow and would block the event loop."""
    return (await asyncio.to_thread(bcrypt.hashpw, _pw(password), bcrypt.gensalt())).decode()


async def verify_password(password: str, hashed: str) -> bool:
    """Constant-time bcrypt check; a malformed stored hash counts as a failed login instead of an error."""
    try:
        return await asyncio.to_thread(bcrypt.checkpw, _pw(password), hashed.encode())
    except ValueError:
        return False


# ------------------------------------------------------------- session JWTs
SESSION_TTL = 12 * 3600


def create_access_token(user_id: str, role: str) -> str:
    """Session JWT for the Streamlit app. The bank-api audience keeps it from being accepted as an MCP token."""
    now = int(time.time())
    return jwt.encode(
        {"sub": user_id, "role": role, "aud": "bank-api", "iat": now, "exp": now + SESSION_TTL},
        settings.jwt_secret,
        algorithm="HS256",
    )


def decode_access_token(token: str) -> dict:
    """Verify signature, expiry and audience of a session JWT; raises jwt.PyJWTError otherwise."""
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"], audience="bank-api")


# ------------------------------------------- OAuth token encryption (per user)


def _fernet(user_id: str, service: str) -> Fernet:
    """Key is derived from (master key, user, service): ciphertext written for
    user A cannot be decrypted with user B's derived key, even if rows are swapped."""
    okm = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"bank-rag/oauth/v1",
        info=f"{user_id}|{service}".encode(),
    ).derive(settings.token_encryption_key.encode())
    return Fernet(base64.urlsafe_b64encode(okm))


def encrypt_token(user_id: str, service: str, payload: dict) -> str:
    """Encrypt an OAuth token payload with the Fernet key derived for exactly this user and service."""
    return _fernet(user_id, service).encrypt(json.dumps(payload).encode()).decode()


def decrypt_token(user_id: str, service: str, blob: str) -> dict:
    """Decrypt a stored token blob. A blob written for another user or service fails authentication here,
    so a mis-addressed row can never be used on someone else's behalf.
    """
    try:
        return json.loads(_fernet(user_id, service).decrypt(blob.encode()))
    except InvalidToken as exc:
        raise ValueError("token blob does not belong to this user/service") from exc


# ------------------------------------------------ uploaded files (encrypted at rest)
FILE_MAGIC = b"BRAG1\n"


def _file_fernet() -> Fernet:
    okm = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"bank-rag/files/v1", info=b"uploaded-file").derive(settings.token_encryption_key.encode())
    return Fernet(base64.urlsafe_b64encode(okm))


def encrypt_bytes(data: bytes) -> bytes:
    """Uploaded Excel/CSV files are sensitive: they are stored as MAGIC + Fernet(AES-128-CBC + HMAC-SHA256) ciphertext."""
    return FILE_MAGIC + _file_fernet().encrypt(data)


def decrypt_bytes(blob: bytes) -> bytes:
    """Inverse of encrypt_bytes; plaintext files written before encryption existed are returned as-is."""
    return _file_fernet().decrypt(blob[len(FILE_MAGIC):]) if blob.startswith(FILE_MAGIC) else blob


# ------------------------------------------- API -> MCP server identity tokens
INTERNAL_TTL = 60


def make_internal_token(user_id: str, service: str, ttl: int = INTERNAL_TTL) -> str:
    """Short-lived JWT the API sends to an MCP server: it names the user (sub) and the one service (aud)
    the call is for, so an MCP server never trusts a user id passed as a tool argument.
    """
    now = int(time.time())
    return jwt.encode(
        {
            "sub": user_id,
            "aud": f"mcp:{service}",
            "iss": "bank-api",
            "iat": now,
            "exp": now + ttl,
            "jti": uuid.uuid4().hex,
        },
        settings.mcp_internal_secret,
        algorithm="HS256",
    )


def internal_claims(token: str, service: str) -> dict:
    """Verified claims of an internal token (signature, audience, issuer, expiry)."""
    return jwt.decode(
        token,
        settings.mcp_internal_secret,
        algorithms=["HS256"],
        audience=f"mcp:{service}",
        issuer="bank-api",
    )


def verify_internal_token(token: str, service: str) -> str:
    """Return the user_id the MCP call is made on behalf of."""
    return internal_claims(token, service)["sub"]
