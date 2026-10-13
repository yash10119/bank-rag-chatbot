"""Pre-flight check for real Google / Slack OAuth. Run:  python scripts/oauth_preflight.py   (or: make oauth-check)

Reads .env, prints the exact redirect URIs / scopes to register in the Google Cloud and Slack consoles, reports what is
still missing, and (if the stack is running) checks that the connect flow really redirects to the provider.
"""
import json
import sys
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
env: dict[str, str] = {}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines() if (ROOT / ".env").exists() else []:
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.split("  #")[0].strip()

ok = True


def check(label: str, good: bool, hint: str = "") -> None:
    global ok
    ok &= good
    print(f"  [{'OK' if good else '!!'}] {label}" + ("" if good else f"  -> {hint}"))


gmail = env.get("MCP_PUBLIC_GMAIL", "http://localhost:8101").rstrip("/")
cal = env.get("MCP_PUBLIC_CALENDAR", "http://localhost:8102").rstrip("/")
slack_redirect = env.get("SLACK_REDIRECT_URI", "")

print("\n== Register these in the consoles ==")
print("Google Cloud Console > Credentials > OAuth client (Web application) > Authorized redirect URIs:")
print(f"    {gmail}/oauth/callback")
print(f"    {cal}/oauth/callback")
print("  Scopes (Data Access): gmail.send, gmail.readonly, calendar.events, calendar.freebusy  (+ openid, email)")
print("  Audience: External, status Testing -> add EVERY user's Gmail address under 'Test users'.")
print("Slack app > OAuth & Permissions > Redirect URLs (must be https):")
print(f"    {slack_redirect or 'https://<your-tunnel-or-domain>/oauth/callback'}")
print("  Bot Token Scopes: chat:write, chat:write.public, channels:read, users:read, users:read.email")

print("\n== Configuration ==")
check("MCP_MOCK=false (real services)", env.get("MCP_MOCK", "true").lower() == "false", "set MCP_MOCK=false in .env")
check("GOOGLE_CLIENT_ID", bool(env.get("GOOGLE_CLIENT_ID")), "paste the OAuth client ID")
check("GOOGLE_CLIENT_SECRET", bool(env.get("GOOGLE_CLIENT_SECRET")), "paste the OAuth client secret")
check("SLACK_CLIENT_ID", bool(env.get("SLACK_CLIENT_ID")), "from Slack app > Basic Information")
check("SLACK_CLIENT_SECRET", bool(env.get("SLACK_CLIENT_SECRET")), "from Slack app > Basic Information")
check("SLACK_REDIRECT_URI is https", slack_redirect.startswith("https://") and "your-tunnel" not in slack_redirect,
      "Slack requires HTTPS: tunnel port 8103 (ngrok http 8103) or use your deployed domain")
for name in ("JWT_SECRET", "TOKEN_ENCRYPTION_KEY", "MCP_INTERNAL_SECRET"):
    check(f"{name} is not the placeholder", env.get(name, "").strip() not in ("", "change-me-long-random") and len(env.get(name, "")) >= 32, "generate: python -c \"import secrets;print(secrets.token_urlsafe(48))\"")

print("\n== Live check (needs `make start`) ==")
try:
    def post(path, data=None, token=None):
        req = urllib.request.Request("http://localhost:8000" + path, data=json.dumps(data or {}).encode(), method="POST",
                                     headers={"content-type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
        return json.load(urllib.request.urlopen(req, timeout=10))

    email, pw = "oauth-preflight@example.com", "Preflight-Pass-123"
    try:
        token = post("/auth/register", {"email": email, "name": "Preflight", "password": pw})["token"]
    except Exception:
        token = post("/auth/login", {"email": email, "password": pw})["token"]

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    for service in ("gmail", "calendar", "slack"):
        try:
            url = post(f"/oauth/{service}/start", token=token)["url"]
        except Exception as exc:
            check(f"{service}: connect link", False, f"API refused: {getattr(exc, 'read', lambda: b'')().decode()[:140] or exc}")
            continue
        try:
            opener.open(url, timeout=10)
            check(f"{service}: MCP /oauth/start redirects", False, "no redirect returned")
        except urllib.error.HTTPError as exc:
            loc = exc.headers.get("location", "")
            host = urlparse(loc).netloc
            want = "slack.com" if service == "slack" else "accounts.google.com"
            q = parse_qs(urlparse(loc).query)
            check(f"{service}: redirects to {host}", exc.code == 302 and want in host and "state" in q, f"got {exc.code} {loc[:100]}")
except Exception as exc:
    print(f"  (skipped: stack not reachable - {type(exc).__name__}; run `make start` first)")

print("\nREADY - open http://localhost:8501 and click Connect." if ok else "\nNot ready yet: fix the [!!] items above.")
sys.exit(0 if ok else 1)
