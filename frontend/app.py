import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from api import Api, ApiError

st.set_page_config(page_title="Banking Assistant", page_icon="🏦", layout="wide")

STEP_ICON = {"running": "⏳", "done": "✅", "waiting": "🧑‍💼", "skipped": "⏭️", "failed": "⚠️"}
SERVICE_ICON = {"gmail": "📧", "calendar": "📅", "slack": "💬"}


def api() -> Api:
    return Api(st.session_state.get("token"))


COOKIE = "bank_session"
SESSION_SECONDS = 12 * 3600  # matches the API's session JWT lifetime


def _cookie_script(token: str | None) -> None:
    """Set or clear the session cookie in the browser. Streamlit cannot set cookies from Python, so a zero-height
    component runs a tiny script (same origin as the app). SameSite=Strict keeps it out of cross-site requests;
    Secure is added on https. The value is the user's own session JWT, so a refresh restores the login."""
    value, age = (token, SESSION_SECONDS) if token else ("", 0)
    components.html(
        f"""<script>
        const secure = (window.parent.location.protocol === 'https:') ? '; Secure' : '';
        const c = "{COOKIE}={value}; path=/; max-age={age}; SameSite=Strict" + secure;
        try {{ window.parent.document.cookie = c; }} catch (e) {{ document.cookie = c; }}
        </script>""",
        height=0,
    )


def start_session(res: dict) -> None:
    """Log in (password, registration or OAuth resume ticket) and remember it across browser refreshes."""
    st.session_state.pop("_signed_out", None)
    st.session_state.update(token=res["token"], user=res["user"], _cookie_action=("set", res["token"]))


def end_session() -> None:
    st.session_state.clear()
    # _signed_out stops restore_login() re-using the cookie that this websocket session received when it connected
    st.session_state.update(_signed_out=True, _cookie_action=("clear", None))


def restore_login() -> None:
    """After a browser refresh Streamlit starts an empty session; take the user back in from the session cookie."""
    if "token" in st.session_state or st.session_state.get("_signed_out"):
        return
    token = st.context.cookies.get(COOKIE)
    if not token:
        return
    try:
        st.session_state.update(token=token, user=Api(token).get("/auth/me"))
    except ApiError:  # expired / revoked / unknown user: forget the cookie and show the login form
        st.session_state["_cookie_action"] = ("clear", None)


def flush_cookie_action() -> None:
    action = st.session_state.pop("_cookie_action", None)
    if action:
        _cookie_script(action[1] if action[0] == "set" else None)


def fail(exc: ApiError) -> None:
    if exc.status == 401:
        for k in ("token", "user", "conversation_id"):
            st.session_state.pop(k, None)
        st.warning("Your session expired. Please sign in again.")
        st.rerun()
    st.error(exc.detail)


# ------------------------------------------------------------------- sign in


def auth_screen() -> None:
    st.title("🏦 Banking Assistant")
    st.caption("Sign in to chat with your bank's assistant, connect your own Gmail, Calendar and Slack, and upload your files.")
    login, register = st.tabs(["Sign in", "Create account"])
    with login, st.form("login"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in", type="primary"):
            try:
                res = Api().post("/auth/login", json={"email": email, "password": password})
                start_session(res)
                st.rerun()
            except ApiError as exc:
                st.error(exc.detail)
    with register, st.form("register"):
        name = st.text_input("Full name")
        email = st.text_input("Email", key="reg_email")
        password = st.text_input("Password (min 8 characters)", type="password", key="reg_pw")
        if st.form_submit_button("Create account", type="primary"):
            try:
                res = Api().post("/auth/register", json={"name": name, "email": email, "password": password})
                start_session(res)
                st.rerun()
            except ApiError as exc:
                st.error(exc.detail)


# ------------------------------------------------------------------- sidebar


@st.fragment(run_every=4)
def connections_panel() -> None:
    """Polls while an authorization tab is open so the status flips to Connected by itself."""
    try:
        services = api().get("/oauth/services")
    except ApiError as exc:
        st.error(exc.detail)
        return
    if any(i.get("simulated") for i in services.values()):
        st.warning("**Demo mode.** Gmail, Calendar and Slack are *simulated* (MCP_MOCK=true): nothing is sent to real accounts. "
                   "See the 📬 Mock outbox tab. Set MCP_MOCK=false and add OAuth credentials to connect real accounts.", icon="🧪")
    for name, info in services.items():
        icon = SERVICE_ICON.get(name, "🔌")
        if info.get("simulated"):
            st.markdown(f"{icon} **{info['label']}** 🧪 simulated  \n<small>No real account connected</small>", unsafe_allow_html=True)
            continue
        if info.get("expired"):
            st.markdown(f"{icon} **{info['label']}** 🟠 token expired  \n<small>{info['account']}</small>", unsafe_allow_html=True)
            st.caption("Access was revoked or expired. Reconnect to continue.")
            if st.button(f"Reconnect {info['label']}", key=f"reconn_{name}", use_container_width=True):
                try:
                    st.session_state[f"auth_url_{name}"] = api().post(f"/oauth/{name}/start")["url"]
                    st.rerun(scope="fragment")
                except ApiError as exc:
                    st.error(exc.detail)
            if url := st.session_state.get(f"auth_url_{name}"):
                st.link_button(f"Authorize {info['label']} ↗", url, use_container_width=True)
            continue
        if info["connected"]:
            st.session_state.pop(f"auth_url_{name}", None)
            c1, c2 = st.columns([3, 1])
            c1.markdown(f"{icon} **{info['label']}** ✅ connected  \n<small>{info['account']}</small>", unsafe_allow_html=True)
            if info["account"].startswith("Local mock"):
                continue  # nothing to disconnect in mock mode
            if c2.button("✕", key=f"disc_{name}", help=f"Disconnect {info['label']}"):
                api().delete(f"/oauth/{name}")
                st.rerun(scope="fragment")
        else:
            st.markdown(f"{icon} **{info['label']}** ⚪ disconnected")
            if not info["configured"]:
                st.caption("⚠️ Not set up yet. A bank officer adds the OAuth client in **⚙️ Integrations setup**.")
            elif url := st.session_state.get(f"auth_url_{name}"):
                st.link_button(f"Authorize {info['label']} ↗", url, use_container_width=True)
                st.caption("Finish in the new tab; this updates automatically.")
            elif st.button(f"Connect {info['label']}", key=f"conn_{name}", use_container_width=True):
                try:
                    st.session_state[f"auth_url_{name}"] = api().post(f"/oauth/{name}/start")["url"]
                    st.rerun(scope="fragment")
                except ApiError as exc:
                    st.error(exc.detail)


def sidebar() -> None:
    user = st.session_state["user"]
    with st.sidebar:
        st.markdown(f"### 👤 {user['name']}")
        st.caption(f"{user['email']} · {'Bank officer' if user['role'] == 'officer' else 'Customer'}")
        if st.button("Sign out", use_container_width=True):
            end_session()
            st.rerun()
        st.divider()
        st.subheader("Your connected accounts")
        st.caption("Each account is yours alone. Tokens are encrypted and never shared with other users.")
        connections_panel()
        with st.expander("Preferences"):
            tz = st.text_input("Timezone", user["timezone"])
            prefs = user.get("prefs", {})
            chans = {k: st.text_input(f"Slack: {label}", prefs.get(k, default)) for k, label, default in [
                ("loans", "loans channel", "#loans-team"), ("compliance", "compliance channel", "#compliance-alerts"),
                ("audit", "audit channel", "#audit-log"), ("rm", "RM notifications", "#rm-notifications")]}
            if st.button("Save preferences"):
                try:
                    st.session_state["user"] = api().patch("/auth/me", json={"timezone": tz, "prefs": chans})
                    st.success("Saved")
                except ApiError as exc:
                    st.error(exc.detail)


# ---------------------------------------------------------------------- chat


def render_analytics(a: dict | None) -> None:
    """Aggregate answers as metric cards + table + chart (not buried in a paragraph)."""
    if not a:
        return
    cards = [c for c in a.get("scalars", []) if c["label"] != "Rows matched"]
    for i in range(0, len(cards), 4):
        cols = st.columns(min(4, len(cards) - i))
        for col, c in zip(cols, cards[i:i + 4]):
            col.metric(c["label"], c["display"])
    t = a.get("table")
    if t and t["rows"]:
        df = pd.DataFrame(t["rows"], columns=t["columns"])
        ch = a.get("chart")
        if ch and len(df) > 1:
            try:
                st.bar_chart(df.set_index(ch["x"])[ch["y"]])
            except Exception:
                pass
        st.dataframe(df, use_container_width=True, hide_index=True)
    d = a.get("detail")
    if d and d["rows"]:
        st.caption(d["title"])
        st.dataframe(pd.DataFrame(d["rows"], columns=d["columns"]), use_container_width=True, hide_index=True)
    if a.get("plan"):
        with st.expander("🧮 How this was computed"):
            st.caption("The model only chose the table, filters and grouping below; the numbers come from an exact pandas computation over every row.")
            st.json({"periods": a.get("periods"), "query": a["plan"]})
    cov = a.get("coverage")
    st.caption(f"Computed exactly over {a.get('rows_matched', 0):,} rows" + (f" · data covers {cov[0]} to {cov[1]}" if cov else ""))


def render_sources(sources: list | None) -> None:
    if not sources:
        return
    seen, parts = set(), []
    for s in sources:
        rows = s.get("rows")
        if isinstance(rows, list) and rows and rows[0] is not None:
            rows = f"rows {rows[0]}–{rows[1]}"
        label = f"`{s.get('file')}`" + (f" ({s['sheet']})" if s.get("sheet") not in (None, "csv") else "") + (f" · {rows}" if rows else "")
        if label not in seen:
            seen.add(label)
            parts.append(label)
    st.caption("📎 Sources: " + "  |  ".join(parts[:6]))


def render_trace(trace: dict | None) -> None:
    if not trace:
        return
    render_analytics(trace.get("analytics"))
    render_sources(trace.get("sources"))
    if not trace.get("steps"):
        return
    with st.expander("🔍 Agent activity", expanded=False):
        for s in trace["steps"]:
            st.markdown(f"{STEP_ICON.get(s['status'], '•')} **{s['agent']}** — {s['detail']}")
        for a in trace.get("actions", []):
            icon = {"ok": "✅", "skipped": "⏭️", "failed": "⚠️"}.get(a["status"], "•")
            st.markdown(f"{icon} {SERVICE_ICON.get(a['service'], '')} `{a['service']}.{a['kind']}` — {a['detail']}")


def conversation_picker() -> str | None:
    convs = api().get("/conversations")
    top = st.columns([4, 1])
    if top[1].button("➕ New", use_container_width=True) or not convs:
        st.session_state["conversation_id"] = api().post("/conversations")["id"]
        convs = api().get("/conversations")
    ids = [c["id"] for c in convs]
    current = st.session_state.get("conversation_id")
    if current not in ids:
        current = ids[0]
    labels = {c["id"]: c["title"] for c in convs}
    chosen = top[0].selectbox("Conversation", ids, index=ids.index(current), format_func=lambda i: labels[i], label_visibility="collapsed")
    st.session_state["conversation_id"] = chosen
    return chosen


def chat_tab() -> None:
    try:
        cid = conversation_picker()
        conv = api().get(f"/conversations/{cid}")
    except ApiError as exc:
        return fail(exc)

    if conv["messages"] and st.button("🗑️ Clear this conversation", help="Deletes this conversation's history"):
        try:
            api().delete(f"/conversations/{cid}")
            st.session_state.pop("conversation_id", None)
            st.rerun()
        except ApiError as exc:
            return fail(exc)
    if not conv["messages"]:
        st.info(
            "Try: *“What is the interest rate on a 5-year home loan for a credit score above 750, and can you schedule a call "
            "with my relationship manager this Thursday?”*  \nFirst, load your demo files in the **Documents** tab and connect your accounts in the sidebar."
        )
    for m in conv["messages"]:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
            render_trace(m.get("trace"))

    pending = conv.get("pending")
    if pending and pending["type"] == "approval":
        st.warning("⏳ This conversation is waiting for a bank officer's review. The outcome will appear here.")
        if st.button("Refresh"):
            st.rerun()
    elif pending:
        st.info("✍️ The assistant asked a question — reply below to continue.")

    if prompt := st.chat_input("Ask about rates, eligibility, transactions, or book an appointment…"):
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            status = st.status("Agents working…", expanded=True)
            out, text = st.empty(), ""
            try:
                for ev in api().stream_message(cid, prompt):
                    t = ev["type"]
                    if t == "step":
                        status.write(f"{STEP_ICON.get(ev['status'], '•')} **{ev['agent']}** — {ev['detail']}")
                    elif t == "token":
                        text += ev["text"]
                        out.markdown(text + "▌")
                    elif t == "final":
                        out.markdown(ev["answer"])
                        status.update(label="Done", state="complete", expanded=False)
                    elif t == "interrupt":
                        out.markdown(ev["message"])
                        status.update(label="Waiting for input", state="complete", expanded=False)
                    elif t == "error":
                        status.update(label="Failed", state="error")
                        st.error(ev["message"])
            except ApiError as exc:
                status.update(label="Failed", state="error")
                fail(exc)
                return
        st.rerun()


# ----------------------------------------------------------------- documents


@st.fragment(run_every=3)
def document_list() -> None:
    try:
        docs = api().get("/documents")
    except ApiError as exc:
        return fail(exc)
    if not docs:
        st.caption("No files yet.")
        return
    df = pd.DataFrame(docs)[["filename", "created_at", "scope", "status", "kinds", "rows", "chunks", "error"]]
    df["created_at"] = df["created_at"].str[:19].str.replace("T", " ")
    df = df.rename(columns={"created_at": "uploaded"})
    df["kinds"] = df["kinds"].map(lambda k: ", ".join(k))
    st.dataframe(df, use_container_width=True, hide_index=True)
    mine = [d for d in docs if d["mine"] or st.session_state["user"]["role"] == "officer"]
    if mine:
        c1, c2 = st.columns([3, 1])
        victim = c1.selectbox("Delete a file", [d["id"] for d in mine], format_func=lambda i: next(d["filename"] for d in mine if d["id"] == i), index=None, placeholder="Select…")
        if c2.button("Delete", disabled=victim is None):
            try:
                api().delete(f"/documents/{victim}")
                st.rerun(scope="fragment")
            except ApiError as exc:
                st.error(exc.detail)


def documents_tab() -> None:
    user = st.session_state["user"]
    st.subheader("Upload Excel / CSV files")
    st.caption("Only `.xlsx` and `.csv` are accepted. Your personal files are visible to you only; bank-wide files (rates, fees, compliance) are shared by officers.")
    scope = "customer"
    if user["role"] == "officer":
        scope = st.radio("Visibility", ["customer", "bank"], format_func=lambda s: "Only me" if s == "customer" else "Bank-wide (all customers)", horizontal=True)
    files = st.file_uploader("Choose files", type=["xlsx", "csv"], accept_multiple_files=True)
    c1, c2 = st.columns(2)
    if c1.button("Upload & index", type="primary", disabled=not files):
        for f in files:
            try:
                api().post("/documents", files={"file": (f.name, f.getvalue())}, data={"scope": scope}, timeout=120)
                st.success(f"{f.name}: queued for indexing")
            except ApiError as exc:
                st.error(f"{f.name}: {exc.detail}")
    if c2.button("Load demo customer data", help="Creates a sample profile (Excel) and transaction history (CSV) for your account"):
        try:
            api().post("/documents/demo")
            st.success("Demo profile and transactions queued for indexing")
        except ApiError as exc:
            st.error(exc.detail)
    st.divider()
    st.subheader("Indexed files")
    document_list()


# ------------------------------------------------------------ activity/approvals


KIND_LABEL = {
    "mcp_tool_call": "🔌 MCP tool call", "agent_turn": "🤖 Agent turn", "llm_call": "🧠 LLM call", "query_resolved": "✅ Query resolved",
    "file_uploaded": "📤 File uploaded", "file_rejected": "⛔ File rejected", "file_indexed": "📚 File indexed", "file_failed": "⚠️ Indexing failed",
    "oauth_connected": "🔗 OAuth connected", "oauth_failed": "🔗 OAuth failed", "oauth_disconnected": "🔓 OAuth disconnected",
    "oauth_refreshed": "♻️ Token refreshed", "oauth_refresh_failed": "🟠 Token refresh failed", "approval_decided": "🧑‍💼 Approval decided",
}


def _summary(e: dict) -> tuple[str, str]:
    """(agent/service, one-line outcome) for the activity table."""
    p, k = e["payload"], e["kind"]
    if k == "mcp_tool_call":
        return f"{p.get('service')}.{p.get('tool')}", ("success" if p.get("ok") else f"failed: {p.get('error')}") + f" · {p.get('ms')} ms"
    if k == "llm_call":
        return p.get("model", ""), (f"error {p['error']}" if p.get("error") else "ok") + f" · {p.get('ms')} ms"
    if k == "agent_turn":
        steps = p.get("steps", [])
        return " → ".join(dict.fromkeys(s["agent"] for s in steps)), f"{len(steps)} steps"
    if k == "query_resolved":
        return ", ".join(p.get("agents") or []), f"intents: {', '.join(p.get('intents') or [])} · risk: {p.get('risk')}"
    if k.startswith("file_"):
        return p.get("filename", ""), p.get("reason") or p.get("error") or (f"{p['rows']} rows" if "rows" in p else "ok")
    if k.startswith("oauth_"):
        return p.get("service", ""), p.get("reason") or p.get("account") or "ok"
    return "", ""


def activity_tab() -> None:
    officer = st.session_state["user"]["role"] == "officer"
    c1, c2 = st.columns([3, 1])
    kinds = c1.multiselect("Filter by event type", list(KIND_LABEL), format_func=KIND_LABEL.get, placeholder="All events")
    everyone = c2.toggle("All customers", value=False) if officer else False
    try:
        events = api().get("/activity", params={"all": everyone, "limit": 200, **({"kind": ",".join(kinds)} if kinds else {})})
    except ApiError as exc:
        return fail(exc)
    if not events:
        st.caption("No events yet.")
        return
    rows = []
    for e in events:
        who, outcome = _summary(e)
        rows.append({"time": e["at"][:19].replace("T", " "), "event": KIND_LABEL.get(e["kind"], e["kind"]), "agent / service": who, "outcome": outcome,
                     "customer": "me" if e["mine"] else "other"})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    with st.expander("Event details (JSON)"):
        for e in events[:25]:
            st.caption(f"{e['at'][:19]} · {e['kind']}")
            st.json(e["payload"], expanded=False)


@st.fragment(run_every=10)
def monitoring_tab() -> None:
    """Staff-only operations dashboard (GET /monitor): live runs, queue, MCP/LLM health, recent tool calls."""
    try:
        m = api().get("/monitor")
    except ApiError as exc:
        return fail(exc) if exc.status != 403 else st.info("Monitoring is available to bank officers.")
    a, b, c, d = st.columns(4)
    a.metric("Active agent runs", len(m["active_runs"]))
    b.metric("Queued jobs", m["queue_depth"])
    c.metric("Pending approvals", m["pending_approvals"])
    d.metric("LLM calls (24h)", m["llm"]["calls_24h"], help=f"avg {m['llm']['avg_ms']} ms · max {m['llm']['max_ms']} ms")
    left, right = st.columns(2)
    with left:
        st.subheader("MCP tools (24h)")
        if m["mcp_by_tool"]:
            st.dataframe(pd.DataFrame([{"tool": t, "calls": v["calls"], "failed": v["failed"]} for t, v in m["mcp_by_tool"].items()]), hide_index=True, use_container_width=True)
        else:
            st.caption("No tool calls yet.")
        st.subheader("Documents")
        st.write(m["documents"] or "none")
    with right:
        st.subheader("Events (24h)")
        st.dataframe(pd.DataFrame([{"event": KIND_LABEL.get(k, k), "count": v} for k, v in sorted(m["events_24h"].items(), key=lambda x: -x[1])]), hide_index=True, use_container_width=True)
    st.subheader("Recent tool calls & OAuth events")
    for e in m["recent"][:15]:
        who, outcome = _summary(e)
        st.markdown(f"`{e['at'][11:19]}` {KIND_LABEL.get(e['kind'], e['kind'])} — **{who}** {outcome}")


def approvals_tab() -> None:
    user = st.session_state["user"]
    officer = user["role"] == "officer"
    show = st.radio("Show", ["pending", "all"], horizontal=True)
    try:
        items = api().get("/approvals", params={"status": show})
    except ApiError as exc:
        return fail(exc)
    if not items:
        st.caption("Nothing here." if officer else "You have no escalated requests.")
    for a in items:
        badge = {"pending": "🟡", "approved": "🟢", "rejected": "🔴"}[a["status"]]
        with st.expander(f"{badge} {a['status'].title()} · {a['requester']} · {a['created_at'][:16].replace('T', ' ')}", expanded=a["status"] == "pending"):
            st.markdown("**Why it was escalated:**\n" + "\n".join(f"- {r}" for r in a["reasons"]))
            st.caption(a["summary"])
            if a.get("note"):
                st.markdown(f"*Officer note:* {a['note']}")
            if officer and a["status"] == "pending":
                if a["mine"]:
                    st.info("You cannot decide your own request (four-eyes rule).")
                    continue
                note = st.text_input("Note (optional)", key=f"note_{a['id']}")
                c1, c2 = st.columns(2)
                for col, decision, label, kind in [(c1, "approved", "Approve", "primary"), (c2, "rejected", "Reject", "secondary")]:
                    if col.button(label, key=f"{decision}_{a['id']}", type=kind, use_container_width=True):
                        try:
                            api().post(f"/approvals/{a['id']}/decision", json={"decision": decision, "note": note})
                            st.success("Decision recorded; the customer's conversation is resuming.")
                            st.rerun()
                        except ApiError as exc:
                            st.error(exc.detail)


def mock_tab() -> None:
    """What the mock Gmail / Calendar / Slack MCP servers captured (MCP_MOCK=true)."""
    try:
        data = api().get("/mock/outbox")
    except ApiError as exc:
        return fail(exc)
    if not data["enabled"]:
        st.info("Mock mode is off (MCP_MOCK=false): messages go to your real Gmail, Calendar and Slack.")
        return
    c1, c2 = st.columns([4, 1])
    c1.caption("Everything the assistant sent through the mock MCP servers. Nothing leaves this machine.")
    if c2.button("Clear", use_container_width=True):
        api().delete("/mock/outbox")
        st.rerun()
    groups = {"gmail": "📧 Email", "calendar": "📅 Calendar", "slack": "💬 Slack"}
    for service, title in groups.items():
        items = [i for i in data["items"] if i["service"] == service]
        st.subheader(f"{title} ({len(items)})")
        if not items:
            st.caption("Nothing yet.")
        for i in items:
            p, when = i["payload"], i["at"][:19].replace("T", " ")
            if service == "gmail":
                label = f"{'↙ Inbox' if i['kind'] == 'inbox' else '↗ Sent'} · {p['subject']}"
                with st.expander(f"{label} · {when}"):
                    st.markdown(f"**{'From' if i['kind'] == 'inbox' else 'To'}:** {p.get('from') or ', '.join(p.get('to', []))}")
                    st.text(p["body"])
            elif service == "calendar":
                with st.expander(f"{p['summary']} · {p['start'][:16].replace('T', ' ')} · {when}"):
                    st.markdown(f"**When:** {p['start']} → {p['end']}  \n**Where:** {p.get('location', '')}  \n**Attendees:** {', '.join(p.get('attendees', []))}")
                    st.text(p.get("description", ""))
            else:
                with st.expander(f"{p['channel']} · {p.get('title') or p.get('text', '')[:50]} · {when}"):
                    if i["kind"] == "card":
                        st.markdown(f"**{p['title']}** ({p['severity']})")
                        for k, v in p["fields"].items():
                            st.markdown(f"- **{k}:** {v}")
                    else:
                        st.text(p["text"])


PROVIDER_HELP = {
    "google": ("Google (Gmail + Calendar)", "https://console.cloud.google.com/apis/credentials",
               "Google Cloud Console → APIs & Services → Credentials → **Create credentials → OAuth client ID → Web application**. "
               "Enable the *Gmail API* and *Google Calendar API*, and add each user's Gmail address under *Audience → Test users* while the app is in Testing."),
    "slack": ("Slack", "https://api.slack.com/apps",
              "api.slack.com/apps → your app → **Basic Information** (Client ID / Secret) and **OAuth & Permissions** (redirect URL + bot scopes)."),
}


def setup_tab() -> None:
    """Officer-only: enter the app's OAuth client for each provider. Nothing is read from .env or code; the secret is
    encrypted in the database and never shown again."""
    st.caption("The OAuth client identifies **this application** to Google / Slack. It is not anyone's account: every user still "
               "signs in with their own Google / Slack account and gets their own token.")
    try:
        data = api().get("/oauth/clients")
    except ApiError as exc:
        return fail(exc)
    for provider, info in data.items():
        title, console, how = PROVIDER_HELP[provider]
        with st.container(border=True):
            badge = f"✅ configured · `{info['client_id']}` · from {info['source']}" if info["configured"] else "⚪ not configured"
            st.markdown(f"#### {title}  \n{badge}")
            st.markdown(f"**1. Create the client** — {how} [Open console ↗]({console})")
            if info["redirect_uris"]:
                st.markdown("**2. Register these redirect URIs exactly** (the provider only redirects to addresses registered on your client):")
                st.code("\n".join(info["redirect_uris"]), language=None)
            else:
                st.markdown("**2. Redirect URL** — Slack requires HTTPS. Use your tunnel or domain for the Slack MCP server, e.g. `https://<address>/oauth/callback`.")
            st.markdown("**Scopes requested:** " + ", ".join(f"`{x.rsplit('/', 1)[-1]}`" for x in info["scopes"]))
            with st.form(f"client_{provider}", clear_on_submit=True):
                st.markdown("**3. Paste the client here**")
                cid = st.text_input("Client ID", key=f"cid_{provider}", placeholder="…apps.googleusercontent.com" if provider == "google" else "1234567890.1234567890")
                secret = st.text_input("Client secret", type="password", key=f"sec_{provider}")
                redirect = st.text_input("HTTPS redirect URL", key=f"red_{provider}", placeholder="https://<address>/oauth/callback") if provider == "slack" else None
                if st.form_submit_button("Save" if not info["configured"] else "Replace", type="primary"):
                    try:
                        api().request("PUT", f"/oauth/clients/{provider}", json={"client_id": cid, "client_secret": secret, "redirect_uri": redirect})
                        st.success("Saved and encrypted. Users can now click Connect.")
                        st.rerun()
                    except ApiError as exc:
                        st.error(exc.detail)
            if info["configured"] and info["source"] == "database" and st.button("Remove this client", key=f"rm_{provider}"):
                try:
                    api().delete(f"/oauth/clients/{provider}")
                    st.rerun()
                except ApiError as exc:
                    st.error(exc.detail)


def handle_oauth_return() -> None:
    """The MCP server redirects the browser back with ?oauth=<service>&status=&msg=&resume=<ticket>.
    The redirect started a fresh Streamlit session, so spend the single-use resume ticket to log the user back in."""
    qp = st.query_params
    if "token" not in st.session_state and "resume" in qp:
        try:
            res = Api().post("/auth/resume", json={"ticket": qp["resume"]})
            start_session(res)
        except ApiError:
            st.warning("Your sign-in link expired - please sign in again.")
    if "oauth" in qp:
        (st.success if qp.get("status") == "ok" else st.error)(qp.get("msg", "OAuth finished"))
        for k in ("oauth", "status", "msg", "resume"):
            qp.pop(k, None)


# ---------------------------------------------------------------------- main

restore_login()
handle_oauth_return()
flush_cookie_action()
if "token" not in st.session_state:
    auth_screen()
else:
    sidebar()
    officer_view = st.session_state["user"]["role"] == "officer"
    tabs = st.tabs(["💬 Chat", "📁 Documents", "🧾 Activity log", "🧑‍💼 Approvals", "📬 Mock outbox"] + (["📊 Monitoring", "⚙️ Integrations setup"] if officer_view else []))
    with tabs[0]:
        chat_tab()
    with tabs[1]:
        documents_tab()
    with tabs[2]:
        activity_tab()
    with tabs[3]:
        approvals_tab()
    with tabs[4]:
        mock_tab()
    if officer_view:
        with tabs[5]:
            monitoring_tab()
        with tabs[6]:
            setup_tab()
