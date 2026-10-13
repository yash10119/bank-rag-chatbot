import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "test-master-key")
os.environ.setdefault("MCP_INTERNAL_SECRET", "test-mcp-secret")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_db_oauth_clients(monkeypatch):
    """Unit tests have no database: the OAuth client store reports 'nothing saved' and never caches between tests."""
    from app.oauth import clients

    async def nothing(provider):
        return None

    monkeypatch.setattr(clients, "_load", nothing)
    clients._cache.clear()
    yield
    clients._cache.clear()
