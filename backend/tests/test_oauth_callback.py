"""OAuth2 callback handler: state validation, token storage under the right user, redirect back to Streamlit."""
import json
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.testclient import TestClient

from app.config import settings
from app.mcp_servers.common import register_oauth
from app.oauth import flow, providers
from app.security import internal_claims

USER_A, USER_B = str(uuid.uuid4()), str(uuid.uuid4())
GMAIL_SCOPES = " ".join(["openid", "email", *providers.SERVICES["gmail"]["scopes"][2:]])


class FakeRedis:
    def __init__(self):
        self.d = {}

    async def set(self, k, v, ex=None, nx=False):
        if nx and k in self.d:
            return None
        self.d[k] = v
        return True

    async def getdel(self, k):
        return self.d.pop(k, None)

    async def exists(self, k):
        return k in self.d

    async def delete(self, k):
        self.d.pop(k, None)


class Env:
    def __init__(self, monkeypatch, scopes=GMAIL_SCOPES):
        self.redis, self.saved, self.revoked = FakeRedis(), [], []
        monkeypatch.setattr(flow, "_redis", lambda: self.redis)
        monkeypatch.setattr(settings, "google_client_id", "cid")
        monkeypatch.setattr(settings, "google_client_secret", "secret")
        monkeypatch.setattr(settings, "frontend_url", "http://app.test:8501")

        async def exchange(service, code, verifier):
            self.verifier = verifier
            return {"access_token": "AT", "refresh_token": "RT", "expires_at": 1}, scopes, "user@gmail.com"

        async def save(session, user_id, service, payload, granted, label):
            self.saved.append((str(user_id), service, payload, label))

        async def revoke(service, payload):
            self.revoked.append(service)

        class Ctx:
            async def __aenter__(self_):
                return object()

            async def __aexit__(self_, *a):
                return False

        monkeypatch.setattr(providers, "exchange_code", exchange)
        monkeypatch.setattr(providers, "revoke", revoke)
        monkeypatch.setattr(flow, "save_connection", save)
        monkeypatch.setattr(flow, "SessionLocal", lambda: Ctx())

    async def start(self, user=USER_A, service="gmail") -> str:
        ticket = flow.connect_url(service, user).split("ticket=")[1]
        url = await flow.begin(service, ticket)
        return parse_qs(urlparse(url).query)["state"][0]


@pytest.fixture
def env(monkeypatch):
    return Env(monkeypatch)


async def test_state_is_stored_server_side_bound_to_user_and_service(env):
    state = await env.start()
    stored = json.loads(env.redis.d[f"oauth:state:{state}"])
    assert stored["user_id"] == USER_A and stored["service"] == "gmail" and len(stored["verifier"]) >= 43


async def test_valid_callback_stores_tokens_for_the_user_who_started_the_flow(env):
    state = await env.start(user=USER_A)
    title, _, ok, uid = await flow.finish("gmail", "code123", state, None)
    assert ok and uid == USER_A and "connected" in title
    assert env.saved == [(USER_A, "gmail", {"access_token": "AT", "refresh_token": "RT", "expires_at": 1}, "user@gmail.com")]
    assert env.verifier  # PKCE verifier from the stored state reached the token exchange


async def test_state_is_single_use(env):
    state = await env.start()
    assert (await flow.finish("gmail", "c", state, None))[2] is True
    again = await flow.finish("gmail", "c", state, None)
    assert again[2] is False and len(env.saved) == 1


async def test_unknown_or_forged_state_is_rejected_and_nothing_is_stored(env):
    _, _, ok, uid = await flow.finish("gmail", "c", "forged-state", None)
    assert not ok and uid is None and env.saved == []


async def test_state_started_for_one_service_cannot_complete_another(env):
    state = await env.start(service="gmail")
    _, _, ok, _ = await flow.finish("calendar", "c", state, None)
    assert not ok and env.saved == []


async def test_user_b_cannot_complete_user_a_flow_into_their_own_account(env):
    state_a = await env.start(user=USER_A)
    await flow.finish("gmail", "c", state_a, None)
    assert [s[0] for s in env.saved] == [USER_A]  # the token is stored under the state's user, never the caller


async def test_missing_permission_revokes_token_and_stores_nothing(monkeypatch):
    env = Env(monkeypatch, scopes="openid email https://www.googleapis.com/auth/gmail.send")  # gmail.readonly unticked
    state = await env.start()
    title, msg, ok, _ = await flow.finish("gmail", "c", state, None)
    assert not ok and "gmail.readonly" in msg and env.saved == [] and env.revoked == ["gmail"]


async def test_user_denied_consent(env):
    assert (await flow.finish("gmail", None, None, "access_denied"))[2] is False


async def test_forged_connect_ticket_is_rejected(env):
    with pytest.raises(Exception):
        await flow.begin("gmail", "not-a-jwt")


def test_http_callback_redirects_back_to_streamlit_with_status_and_resume_ticket(monkeypatch):
    env = Env(monkeypatch)
    mcp = FastMCP("t")
    register_oauth(mcp, "gmail")
    client = TestClient(Starlette(routes=mcp._custom_starlette_routes), follow_redirects=False)

    import asyncio
    state = asyncio.run(env.start())
    r = client.get("/oauth/callback", params={"code": "c", "state": state})
    assert r.status_code == 302
    loc = urlparse(r.headers["location"])
    q = parse_qs(loc.query)
    assert f"{loc.scheme}://{loc.netloc}" == "http://app.test:8501" and q["oauth"] == ["gmail"] and q["status"] == ["ok"]
    assert internal_claims(q["resume"][0], "frontend:resume")["sub"] == USER_A  # restores the right user's login

    bad = client.get("/oauth/callback", params={"code": "c", "state": "forged"})
    q2 = parse_qs(urlparse(bad.headers["location"]).query)
    assert bad.status_code == 302 and q2["status"] == ["error"] and "resume" not in q2  # no login for a forged state


def test_http_start_redirects_to_google_with_pkce(monkeypatch):
    Env(monkeypatch)
    mcp = FastMCP("t")
    register_oauth(mcp, "gmail")
    client = TestClient(Starlette(routes=mcp._custom_starlette_routes), follow_redirects=False)
    ticket = flow.connect_url("gmail", USER_A).split("ticket=")[1]
    r = client.get("/oauth/start", params={"ticket": ticket})
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert r.status_code == 302 and urlparse(r.headers["location"]).netloc == "accounts.google.com"
    assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"][0].endswith("/oauth/callback")
