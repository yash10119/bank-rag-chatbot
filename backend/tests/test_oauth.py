import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest

from app.config import settings
from app.oauth import providers
from app.security import decrypt_token, encrypt_token


def test_pkce_challenge_matches_verifier():
    verifier, challenge = providers.new_pkce()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected and len(verifier) >= 43


async def test_google_url_has_state_pkce_offline_and_scopes(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "cid")
    monkeypatch.setattr(settings, "google_client_secret", "sec")
    q = parse_qs(urlparse(await providers.build_authorization_url("gmail", "st4te", "chal")).query)
    assert q["state"] == ["st4te"] and q["code_challenge"] == ["chal"] and q["code_challenge_method"] == ["S256"]
    assert q["access_type"] == ["offline"] and "gmail.send" in q["scope"][0]


async def test_slack_url_uses_comma_scopes(monkeypatch):
    monkeypatch.setattr(settings, "slack_client_id", "sid")
    monkeypatch.setattr(settings, "slack_client_secret", "ssec")
    monkeypatch.setattr(settings, "slack_redirect_uri", "https://x.example/oauth/callback")
    q = parse_qs(urlparse(await providers.build_authorization_url("slack", "s", "c")).query)
    assert "chat:write" in q["scope"][0].split(",")


def test_missing_scopes_detects_unticked_google_permission():
    granted = "openid email https://www.googleapis.com/auth/gmail.send"
    assert providers.missing_scopes("gmail", granted) == ["https://www.googleapis.com/auth/gmail.readonly"]
    full = granted + " https://www.googleapis.com/auth/gmail.readonly"
    assert providers.missing_scopes("gmail", full) == []


def test_missing_scopes_slack_comma_separated():
    assert providers.missing_scopes("slack", "chat:write,chat:write.public,channels:read,users:read,users:read.email") == []
    assert providers.missing_scopes("slack", "chat:write") != []


def test_token_blob_cannot_be_opened_by_another_user_or_service():
    blob = encrypt_token("user-a", "gmail", {"access_token": "t"})
    assert decrypt_token("user-a", "gmail", blob)["access_token"] == "t"
    with pytest.raises(ValueError):
        decrypt_token("user-b", "gmail", blob)
    with pytest.raises(ValueError):
        decrypt_token("user-a", "calendar", blob)


def test_redirect_uris_point_at_the_mcp_servers(monkeypatch):
    monkeypatch.setattr(settings, "mcp_public_gmail", "http://localhost:8101/")
    monkeypatch.setattr(settings, "mcp_public_calendar", "http://localhost:8102")
    assert providers.redirect_uri("gmail") == "http://localhost:8101/oauth/callback"
    assert providers.redirect_uri("calendar") == "http://localhost:8102/oauth/callback"


def test_connect_ticket_is_bound_to_user_and_service(monkeypatch):
    from app.oauth import flow
    from app.security import verify_internal_token

    monkeypatch.setattr(settings, "mcp_public_gmail", "http://localhost:8101")
    url = flow.connect_url("gmail", "user-a")
    assert url.startswith("http://localhost:8101/oauth/start?ticket=")
    ticket = url.split("ticket=")[1]
    assert verify_internal_token(ticket, "gmail:connect") == "user-a"
    with pytest.raises(Exception):
        verify_internal_token(ticket, "calendar:connect")  # a gmail ticket is useless on the calendar server
    with pytest.raises(Exception):
        verify_internal_token(ticket, "gmail")  # and is not a valid MCP tool-call token


# ---- OAuth client configured at runtime (Settings page) instead of static .env values ------------------------------


async def test_settings_page_client_wins_over_env_and_is_used_in_the_authorization_url(monkeypatch):
    from app.oauth import clients

    monkeypatch.setattr(settings, "google_client_id", "env-id")
    monkeypatch.setattr(settings, "google_client_secret", "env-secret")

    async def from_db(provider):
        return {"client_id": "db-id.apps.googleusercontent.com", "client_secret": "db-secret", "source": "database"} if provider == "google" else None

    monkeypatch.setattr(clients, "_load", from_db)
    q = parse_qs(urlparse(await providers.build_authorization_url("gmail", "s", "c")).query)
    assert q["client_id"] == ["db-id.apps.googleusercontent.com"]


async def test_without_any_client_the_flow_refuses_with_a_clear_message(monkeypatch):
    for name in ("google_client_id", "google_client_secret"):
        monkeypatch.setattr(settings, name, "")
    assert await providers.is_configured("gmail") is False
    with pytest.raises(providers.OAuthError, match="not set up yet"):
        await providers.build_authorization_url("gmail", "s", "c")


def test_client_secret_is_encrypted_with_its_own_key_and_never_readable_as_a_user_token():
    from app.oauth import clients

    blob = encrypt_token(clients._KEY_OWNER, "client:google", {"client_id": "i", "client_secret": "s3cret"})
    assert "s3cret" not in blob and decrypt_token(clients._KEY_OWNER, "client:google", blob)["client_secret"] == "s3cret"
    with pytest.raises(ValueError):
        decrypt_token("some-user-id", "gmail", blob)
    assert clients.mask("123456789012-abcdefghijklmnop.apps.googleusercontent.com").startswith("12345678…")
