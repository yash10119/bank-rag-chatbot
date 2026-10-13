"""Generates DesignDoc.pdf (5-8 pages) for the Banking Multi-Agent RAG Chatbot."""
import sys

from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OUT = sys.argv[1] if len(sys.argv) > 1 else "DesignDoc.pdf"
DEMO_URL = sys.argv[2] if len(sys.argv) > 2 else "(fill in after deployment)"
NAVY, TEAL, GREY, LIGHT = colors.HexColor("#12355b"), colors.HexColor("#1b7f79"), colors.HexColor("#5b6770"), colors.HexColor("#eef3f7")

base = ParagraphStyle("b", fontName="Helvetica", fontSize=9.2, leading=12.2, alignment=TA_LEFT, spaceAfter=2.6)
small = ParagraphStyle("s", parent=base, fontSize=8.0, leading=10.0, spaceAfter=0)
bullet = ParagraphStyle("bl", parent=base, leftIndent=9, bulletIndent=1, spaceAfter=1.4)
h1 = ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=12.6, leading=15, textColor=NAVY, spaceBefore=6.5, spaceAfter=3)
h2 = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=9.8, leading=12.2, textColor=TEAL, spaceBefore=3.5, spaceAfter=1.6)
title = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=19, leading=22, textColor=NAVY, spaceAfter=3)
sub = ParagraphStyle("st", fontName="Helvetica", fontSize=9.2, leading=12, textColor=GREY, spaceAfter=6)
code = ParagraphStyle("c", parent=base, fontName="Courier", fontSize=7.2, leading=8.8, backColor=LIGHT, borderPadding=(2, 3, 2, 3), spaceBefore=2, spaceAfter=4)

story = []


def H1(t):
    story.append(Paragraph(t, h1))


def H2(t):
    story.append(Paragraph(t, h2))


def P(t, style=base):
    story.append(Paragraph(t, style))


def B(items):
    for it in items:
        story.append(Paragraph(it, bullet, bulletText="•"))


def table(rows, widths, header=True, font=7.9):
    cell = ParagraphStyle("cell", parent=small, fontSize=font, leading=font + 1.7)
    data = [[Paragraph(str(c), cell) for c in r] for r in rows]
    t = Table(data, colWidths=[w * mm for w in widths], repeatRows=1 if header else 0)
    st = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c5d0d8")),
          ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 1.8), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.8)]
    if header:
        st += [("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white)]
        for c in data[0]:
            c.style = ParagraphStyle("hd", parent=cell, textColor=colors.white, fontName="Helvetica-Bold")
    t.setStyle(TableStyle(st))
    story.append(t)
    story.append(Spacer(1, 3))


# ------------------------------------------------------------------ diagrams


def box(d, x, y, w, h, text, fill=LIGHT, stroke=NAVY, size=6.6, bold=False):
    d.add(Rect(x, y, w, h, fillColor=fill, strokeColor=stroke, strokeWidth=0.7, rx=2, ry=2))
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        d.add(String(x + w / 2, y + h / 2 + (len(lines) / 2 - i - 0.78) * (size + 1.4), ln, fontName="Helvetica-Bold" if bold and i == 0 else "Helvetica", fontSize=size, fillColor=colors.black, textAnchor="middle"))


def arrow(d, x1, y1, x2, y2, label="", dashed=False, color=NAVY):
    d.add(Line(x1, y1, x2, y2, strokeColor=color, strokeWidth=0.8, strokeDashArray=[2, 2] if dashed else None))
    import math
    a = math.atan2(y2 - y1, x2 - x1)
    p = 4.2
    d.add(Polygon([x2, y2, x2 - p * math.cos(a - 0.4), y2 - p * math.sin(a - 0.4), x2 - p * math.cos(a + 0.4), y2 - p * math.sin(a + 0.4)], fillColor=color, strokeColor=color))
    if label:
        d.add(String((x1 + x2) / 2 + 2, (y1 + y2) / 2 + 2, label, fontName="Helvetica-Oblique", fontSize=5.6, fillColor=GREY))


def architecture():
    d = Drawing(500, 262)
    G = colors.HexColor("#e3f3f1")
    OR = colors.HexColor("#c58a1a")
    box(d, 4, 214, 62, 30, "User browser", fill=colors.white)
    box(d, 86, 204, 104, 50, "Streamlit  :8501\nChat · Files · Settings\nActivity · Monitoring", bold=True)
    box(d, 222, 78, 150, 176, "", fill=colors.white)
    d.add(String(297, 240, "FastAPI :8000 (fully async)", fontName="Helvetica-Bold", fontSize=7.2, textAnchor="middle"))
    for i, t in enumerate(["auth · documents · chat (SSE)", "OAuth connect tickets / resume", "LangGraph runtime", "+ Postgres checkpointer", "ingest / audit / monitor APIs"]):
        d.add(String(297, 222 - i * 12, t, fontName="Helvetica", fontSize=6.4, textAnchor="middle"))
    right = [("LLM provider\nOllama / OpenAI-compatible", 224), ("arq worker\ningest · resume · audit mirror", 188), ("Redis\nbroker · OAuth state · locks", 152),
             ("Qdrant\ndense + BM25 vectors", 116), ("Postgres\nusers · tokens · audit · checkpoints", 80)]
    for t, y in right:
        box(d, 404, y, 92, 30, t, size=5.9)
        arrow(d, 372, y + 15, 404, y + 15)
    arrow(d, 66, 229, 86, 229)
    arrow(d, 190, 229, 222, 229, "HTTP / SSE")
    mc = [("mcp-gmail :8101", 40), ("mcp-calendar :8102", 160), ("mcp-slack :8103", 280)]
    for n, x in mc:
        box(d, x, 8, 108, 44, n + "\n/oauth/start · /oauth/callback\nMCP tools (verify JWT,\nload + refresh user token)", fill=G, stroke=TEAL, bold=True, size=5.9)
        arrow(d, x + 54, 52, 222 + (x - 40) / 240 * 150 + 0, 78, color=TEAL)
    box(d, 404, 8, 92, 44, "Google APIs\nGmail · Calendar\nSlack API", fill=colors.HexColor("#fff4df"), stroke=OR)
    arrow(d, 388, 30, 404, 30, color=OR)
    d.add(String(150, 62, "60-s JWT per tool call (sub = user_id)", fontName="Helvetica-Oblique", fontSize=5.8, fillColor=GREY))
    arrow(d, 34, 214, 70, 52, "", dashed=True, color=TEAL)
    d.add(String(8, 124, "browser OAuth redirect:", fontName="Helvetica-Oblique", fontSize=5.8, fillColor=TEAL))
    d.add(String(8, 116, "Connect → MCP /oauth/start →", fontName="Helvetica-Oblique", fontSize=5.8, fillColor=TEAL))
    d.add(String(8, 108, "provider → MCP /oauth/callback", fontName="Helvetica-Oblique", fontSize=5.8, fillColor=TEAL))
    d.add(String(8, 100, "→ back to Streamlit", fontName="Helvetica-Oblique", fontSize=5.8, fillColor=TEAL))
    return d


def graph():
    d = Drawing(500, 168)
    y = 126
    for n, x, w in (("intake", 2, 44), ("risk_gate", 58, 52), ("dispatch\n(Send fan-out)", 122, 66)):
        box(d, x, y, w, 28, n, bold=True)
    arrow(d, 46, y + 14, 58, y + 14)
    arrow(d, 110, y + 14, 122, y + 14)
    box(d, 2, 78, 52, 28, "clarify\nHITL: customer", fill=colors.HexColor("#fff4df"), size=5.9)
    arrow(d, 24, y, 24, 106, "ambiguous")
    arrow(d, 54, 92, 46, y + 4)
    box(d, 60, 72, 84, 34, "request_approval\n→ await_approval\nHITL: officer (durable)", fill=colors.HexColor("#fff4df"), size=5.8)
    arrow(d, 84, y, 96, 106, "high risk")
    specs = ["rates_agent", "eligibility_agent", "transaction_agent", "analytics_agent", "inbox_agent", "faq_agent"]
    for i, n in enumerate(specs):
        yy = 150 - i * 24
        box(d, 208, yy - 12, 84, 18, n, fill=colors.HexColor("#e3f3f1"), stroke=TEAL, size=6.4)
        arrow(d, 188, y + 14, 208, yy - 3)
        arrow(d, 292, yy - 3, 316, 98)
    for nm, x, w in (("scheduler\nCalendar MCP", 316, 56), ("notify\nGmail + Slack MCP", 384, 62), ("respond\n→ audit", 458, 40)):
        box(d, x, 82, w, 32, nm, bold=True, size=6.0)
    arrow(d, 372, 98, 384, 98)
    arrow(d, 446, 98, 458, 98)
    d.add(String(2, 8, "Conditional edges: intake→clarify|risk_gate · risk_gate→respond|request_approval|specialists · approval→respond|specialists.", fontName="Helvetica-Oblique", fontSize=5.8, fillColor=GREY))
    d.add(String(2, 0, "Specialists run in parallel; evidence merges into state; clarify and approval pause the thread via interrupt().", fontName="Helvetica-Oblique", fontSize=5.8, fillColor=GREY))
    return d


# ------------------------------------------------------------------ content

story.append(Paragraph("Banking Multi-Agent RAG Chatbot — Design Document", title))
story.append(Paragraph(f"LangGraph · MCP (Gmail, Calendar, Slack) · per-user OAuth2 · Excel/CSV-only RAG · FastAPI · Streamlit &nbsp;|&nbsp; Live demo: {DEMO_URL} &nbsp;|&nbsp; API docs: /docs", sub))

H1("1. System Architecture")
story.append(architecture())
P("<b>Request path.</b> The browser talks only to Streamlit. Streamlit holds the user's session JWT in <font face='Courier'>st.session_state</font> and calls FastAPI over HTTP; chat uses server-sent events "
  "(<font face='Courier'>step | token | final | interrupt</font>) so the UI can show which agent is working. FastAPI runs the LangGraph graph in-process with a Postgres checkpointer; heavy or deferred work (embedding, "
  "resuming a graph after an officer approves, mirroring audit summaries to Slack) goes through <b>arq on Redis</b> to a worker. Agents reach Gmail/Calendar/Slack only through three MCP servers over streamable HTTP; each call carries a "
  "60-second JWT (<font face='Courier'>sub=user_id, aud=mcp:&lt;service&gt;</font>) minted by the API from the <i>graph state's</i> user id, never from model output. The MCP server, not the API, loads and decrypts that user's token.")
H2("Process — File ingestion & validation")
B(["<b>UI:</b> <font face='Courier'>st.file_uploader(type=['xlsx','csv'])</font> blocks other extensions in the widget. <b>Server:</b> <font face='Courier'>validate_upload</font> (first step of <font face='Courier'>save_and_queue</font>) checks the extension <i>and</i> content "
   "(xlsx must be a zip with <font face='Courier'>xl/workbook.xml</font>; csv must not be binary/zip, so a renamed PDF fails). Rejection returns HTTP 415 and writes a <font face='Courier'>file_rejected</font> audit row. Tested in <font face='Courier'>test_rag_pipeline.py</font>.",
   "Accepted bytes are written <b>encrypted at rest</b> (Fernet) under a server-generated name, a <font face='Courier'>documents</font> row is created, and an arq job is queued; the worker parses → classifies → chunks → embeds → upserts to Qdrant and sets status <i>ready</i> (or <i>failed</i> + error, audited)."])
H2("Process — User query → agent routing")
B(["Streamlit POSTs <font face='Courier'>/conversations/{id}/messages</font>; the API takes a Redis lock (one in-flight run per conversation), saves the message and starts the graph on thread <font face='Courier'>&lt;user_id&gt;:&lt;conversation_id&gt;</font>. "
   "<b>intake</b> (LLM structured output → <font face='Courier'>Understanding</font>: intents, entities, dates) classifies intent; <b>risk_gate</b> applies rules; <b>dispatch</b> fans out to the specialists the intents need; results merge into <font face='Courier'>state.evidence</font>; "
   "<b>respond</b> streams the reply back as SSE; <b>audit</b> persists the turn. Section 3 details the topology."])

H1("2. RAG Pipeline Design (Excel & CSV only)")
table([["Stage", "Design and reason"],
       ["Boundary", "Enforced at two layers (widget + <font face='Courier'>validate_upload</font>) and again implicitly: <font face='Courier'>load_tables</font> is the <i>only</i> reader and only understands pandas CSV / openpyxl. Nothing else can reach the index."],
       ["Parsing", "All cells read as strings (no silent type coercion of IDs/dates); headers normalised; empty rows/columns dropped; encoding fallback (utf-8-sig → latin-1); row cap (100k) rejects abusive files. <b>Every sheet</b> of a workbook is its own table; each is tagged with file + sheet in every chunk so a 'Home Loan' sheet and a 'Personal Loan' sheet never blur."],
       ["Table classification", "Heuristic on column names → rates / fees / compliance / eligibility_rules / profile / transactions / other. Used to filter retrieval (the rates agent never reads transaction rows) and to pick the chunk strategy."],
       ["Chunking", "Table-aware, three kinds: <b>schema</b> chunk per sheet (columns, numeric ranges, categorical values — answers 'what does this file contain'); <b>row</b> chunks rendered <font face='Courier'>[file &gt; sheet] col: value; …</font> (one row per chunk, grouped ≤40 only for very large tables so a 100k-row file stays within MAX_ROW_CHUNKS); <b>monthly summary</b> chunks for transaction tables. Rows are kept intact because a half-row is meaningless."],
       ["Embedding", "<b>Hybrid</b>: dense <font face='Courier'>BAAI/bge-small-en-v1.5</font> (384-d, ONNX via fastembed, local) + sparse <font face='Courier'>Qdrant/bm25</font>. Dense captures meaning ('mortgage' ≈ 'home loan'); BM25 keeps exact tokens ('750', 'APT-2026…', 'BR001') that dense models blur — essential for tabular data. Local models mean no customer data leaves the host to embed."],
       ["Retrieval", "Qdrant hybrid query with <b>RRF fusion</b>, filtered in the store by <font face='Courier'>scope=bank OR (scope=customer AND owner_id=caller)</font>; callers cannot widen it. Retrieved rows are numbered <font face='Courier'>[S#]</font> in the prompt; the model must cite tags or say 'not found'. Sources (file, sheet, row range) are shown under each answer."],
       ["Exact reads", "Numbers are never left to an LLM: profile fields, transaction anomaly flags (large transfer vs the threshold read from the compliance CSV, 3σ outliers, rapid succession, repeated amounts) and all aggregates are computed with pandas (Section 4)."]],
      [27, 150])

H1("3. Multi-Agent Graph Design (LangGraph)")
story.append(graph())
B(["<b>Nodes:</b> intake, clarify, risk_gate, request_approval, await_approval, dispatch (router), six specialists (rates, eligibility, transaction, <b>analytics</b>, inbox, faq), scheduler, notify, respond, audit. "
   "<b>Conditional edges</b> implement real branching: ambiguity → clarify loop; out-of-scope → respond; high risk → approval; <font face='Courier'>dispatch</font> returns a list of <font face='Courier'>Send</font> objects so the needed specialists run <b>in parallel</b> "
   "(a loan-rate + appointment message runs rates + eligibility concurrently, then scheduler, then notify).",
   "<b>State</b> (<font face='Courier'>BankState</font>): identity fields set by the API (user_id, email, tz), <font face='Courier'>messages</font> (add_messages reducer), per-turn working memory reset by intake (<font face='Courier'>evidence</font> merge-dict so parallel agents write disjoint keys; <font face='Courier'>actions</font>/<font face='Courier'>trace</font> append-lists), approval and appointment records. Multi-turn memory = the checkpointed thread.",
   "<b>Human-in-the-loop:</b> (1) <i>customer</i> — ambiguous requests (e.g. a transfer with no amount) call <font face='Courier'>interrupt()</font> in <font face='Courier'>clarify</font>; the next chat message resumes the graph. (2) <i>officer</i> — transfers ≥ the compliance threshold, fraud reports, large disputes create an <font face='Courier'>approvals</font> row, alert #compliance-alerts, "
   "and pause durably in <font face='Courier'>await_approval</font> (state lives in Postgres, survives restarts). An officer decides in the UI (four-eyes: not their own request); the API enqueues a worker job that resumes the thread and posts the outcome to the customer's conversation. The bot never claims to move money.",
   "<b>Failure modes:</b> every MCP call returns <font face='Courier'>McpResult(ok, error)</font> and never raises for expected failures; a Calendar timeout → <font face='Courier'>appointment.status=failed</font>, the reply says so, other actions still run; a missing connection is reported as <i>skipped</i> with a 'connect it in the sidebar' message (never silent). "
   "Scheduling is idempotent (event id derived from the booking reference) and double-booking is prevented by a DB reservation under a per-RM advisory lock plus a Calendar availability re-check inside <font face='Courier'>create_event</font>. A Redis lock keeps one run per thread. "
   "Dates like 'this Thursday' are resolved in code (<font face='Courier'>resolve_relative_date</font>), not by the model."])

H1("4. Aggregated Query Handling (process + answers to the assessment's questions)")
B(["<b>Detection.</b> Intake classifies <font face='Courier'>analytics_query</font> (totals, counts, averages, rankings, trends, comparisons); a regex backstop (aggregate word + a parsed period) catches phrasing small models miss. "
   "<b>Structured compute, not LLM arithmetic:</b> an LLM <i>planner</i> fills a typed <font face='Courier'>AggPlan</font> (table, date column, filters, group-by incl. derived month/quarter/year, metrics sum/count/count_distinct/avg/min/max, join, denominator, comparison) for the 4 most relevant tables; "
   "<font face='Courier'>pandas</font> executes it over <b>every row</b> of the full files; the reply text is generated from the numbers, not by the LLM. Metric cards, a table and a chart are rendered from the same result.",
   "<b>Dates</b> come from a deterministic parser (<font face='Courier'>periods.py</font>: Q1 2026, April to June, last quarter, first half, last 3 months, financial year with configurable start month) — a wrong date silently changes a banking total, so the model never writes one; model-written date filters are discarded.",
   "<b>Multi-file questions.</b> <i>Join:</i> 'branch' lives in Accounts, loans only have account_id → the plan joins on the shared key (left join, right side de-duplicated, noted if keys repeat). <i>Ratio:</i> 'recovered as % of total outstanding' = sub-query A / sub-query B over two files (<font face='Courier'>denominator</font>, with or without the period). "
   "<i>Comparison:</i> 'top branch Q1 vs Q4' computes the top group, then the same groups in the previous/second period (delta and %). Multi-sheet workbooks with identical columns also expose a combined <font face='Courier'>file::*</font> table.",
   "<b>Guardrails against hallucinated aggregates.</b> (1) Plans are validated against the real schema — unknown table/column ⇒ error fed back for <i>one</i> retry, then an explicit 'I couldn't compute that exactly' (no estimate). (2) <font face='Courier'>repair_plan</font> fixes predictable model slips from the question text (prose in a numeric filter, counting instead of summing, identifier group-by, missing join) and every repair is shown to the user under 'How this was computed'. "
   "(3) Zero matches, a period beyond the data's date coverage, and un-filtered statuses are stated explicitly; empty months are shown as 0 only up to the data's last month. (4) Filter values are parsed strictly (Rs / lakh / crore) — the first live test showed a lenient parser reading prose as 0.1.",
   "<b>Accuracy/latency trade-off.</b> Exactness costs one planner LLM call (+ one retry on invalid plans) and a full-file pandas pass (~tens of ms for 100k rows) instead of a single retrieval; with a local 14B model on CPU planning dominates (~1.5-3 min), with a hosted model seconds. The alternative — retrieving top-K rows and letting the LLM add them up — is faster but wrong by construction, which is unacceptable where an aggregate has compliance consequences. "
   "<b>Verified:</b> results were compared with independent pandas calculations on the supplied dataset (e.g. Apr–Jun debit 48,190,492.81 / credit 53,495,177.93 month by month; top categories; overdue count) and 30+ unit tests cover each query class."])

H1("5. MCP Integration Design")
table([["Server", "Tools", "Why it adds value (not just presence)"],
       ["Gmail", "send_email · list_recent_emails · read_email", "Confirmation to the customer, high-priority notice to the RM, summarising a customer's recent mail (content treated as <i>untrusted data</i> in prompts)."],
       ["Calendar", "check_availability · find_free_slots · create_event", "Availability check before booking; free-slot suggestions on the requested day; invite with branch address, reference number and agenda; conflict ⇒ alternatives, never a double-booking."],
       ["Slack", "post_message · post_structured · notify_user_by_email", "RM DM, #loans-team lead, #compliance-alerts for approvals/high-risk, structured summary of every resolved query to #audit-log. Slack control sequences (&lt;!channel&gt;) are escaped."]],
      [18, 52, 107])
B(["<b>Tool-selection policy</b> is explicit, not left to a free-form model: <i>intents</i> (extracted by intake) decide which node runs — <font face='Courier'>schedule_appointment</font> → scheduler (Calendar); the notify node maps intents/risk to a fixed set (customer email ⇒ Gmail; RM + team ⇒ Slack; high risk ⇒ Slack compliance) and runs them <b>concurrently</b> with <font face='Courier'>asyncio.gather</font>. This is auditable and testable; free tool-calling would let a prompt-injected email trigger actions.",
   "<b>Token injection:</b> graph → <font face='Courier'>call_tool(service, state.user_id, tool, args)</font> → short-lived JWT → MCP server verifies, loads <i>that</i> user's token (refreshing if needed) → provider API. Tools accept no user/token arguments. <b>Failures</b> (unreachable, not connected, provider error) become <font face='Courier'>failed|skipped</font> actions with a human-readable reason shown in the reply and the Activity log; every call is audited (service, tool, outcome, latency)."])

H1("6. OAuth2 Architecture & Token Lifecycle")
P("<b>Two different things.</b> The <i>OAuth client</i> (ID + secret) identifies this <b>application</b> to Google/Slack and is required to start any consent flow; "
  "it grants access to nobody's data. A <i>user token</i> is what a person grants when they sign in. The system has exactly one client per provider and <b>one token per user per service</b> — "
  "there is no service account, no shared mailbox and no token used on behalf of more than one user.")
table([["Step", "What happens (all server-side)"],
       ["0 App client", "A bank officer opens <b>Integrations setup</b>, which lists the exact redirect URIs and scopes to register, and pastes the client ID + secret. It is stored as Fernet ciphertext in <font face='Courier'>oauth_clients</font> (its own HKDF context, so it can never be decrypted as a user token), "
        "is never returned to the browser (ID masked, secret never shown), can be replaced/removed without a restart (15-second cache so the API and the three MCP processes pick it up) and every change is audited. "
        "<font face='Courier'>GOOGLE_CLIENT_ID/SECRET</font> and <font face='Courier'>SLACK_CLIENT_ID/SECRET</font> in <font face='Courier'>.env</font> remain a documented fallback for automated deployments; the database value wins. "
        "Google offers no client-less flow and no dynamic client registration, so one registered client is unavoidable."],
       ["1 Initiate", "User clicks <b>Connect</b> in Streamlit → API <font face='Courier'>POST /oauth/{service}/start</font> signs a 5-minute <i>connect ticket</i> (JWT bound to user + service) → browser opens <font face='Courier'>mcp-&lt;service&gt;/oauth/start?ticket=…</font>. The MCP server verifies it, creates a random <b>state</b> + PKCE verifier stored in Redis (10 min, single-use, bound to user_id+service) and 302s to the provider (Google: <i>offline access, prompt=consent, S256 PKCE</i>; Slack: bot scopes)."],
       ["2 Authorise", "User signs in with <b>their own</b> Google/Slack account and grants the scopes below."],
       ["3 Callback", "Provider → <font face='Courier'>mcp-&lt;service&gt;/oauth/callback</font>. The state is <i>consumed</i> (<font face='Courier'>GETDEL</font>: replay fails), service must match, the code is exchanged (client secret + PKCE verifier), <b>granted scopes are verified</b> (Google's granular consent lets users untick permissions → token revoked and rejected), tokens are encrypted and stored under the state's user. Tokens never reach the browser. The MCP server then 302s to Streamlit with <font face='Courier'>?oauth=&lt;service&gt;&amp;status=ok</font> plus a 2-minute single-use <i>resume ticket</i> that restores the login (the redirect starts a new Streamlit session)."],
       ["4 Use & refresh", "Each MCP call → <font face='Courier'>get_access_token(user, service)</font> under a row lock (<font face='Courier'>SELECT … FOR UPDATE</font>, so concurrent calls don't double-refresh): if the Google access token expires within 60 s, the refresh token silently obtains a new one and re-encrypts it. If refresh fails (revoked), the connection is flagged <b>expired</b> (Redis flag, audited), tools return <font face='Courier'>not_connected</font>, the reply says so and the sidebar card shows <b>🟠 token expired — Reconnect</b>."],
       ["5 Disconnect", "✕ in the sidebar → provider revocation endpoint (Google <font face='Courier'>/revoke</font>, Slack <font face='Courier'>auth.revoke</font>) then the encrypted row is deleted; the card flips to <b>disconnected</b> immediately; audited."],
       ["Storage", "<b>Fernet</b> (AES-128-CBC + HMAC-SHA256). The key is derived <b>per (user, service)</b> by HKDF-SHA256 from <font face='Courier'>TOKEN_ENCRYPTION_KEY</font>, so a blob copied into another user's row cannot be decrypted (unit-tested). Uploaded files use a separate HKDF context."]],
      [20, 157])
table([["Service", "Exact scopes requested", "Justification (minimal for the tools)"],
       ["Gmail", "<font face='Courier'>openid email</font> · <font face='Courier'>gmail.send</font> · <font face='Courier'>gmail.readonly</font>", "openid/email: show the connected address. send: confirmation/RM emails. readonly: list/read mail for 'summarise my emails' (no modify/delete)."],
       ["Google Calendar", "<font face='Courier'>openid email</font> · <font face='Courier'>calendar.events</font> · <font face='Courier'>calendar.freebusy</font>", "events: create the invite. freebusy: availability without reading event details."],
       ["Slack (bot)", "<font face='Courier'>chat:write chat:write.public channels:read users:read users:read.email</font>", "post alerts/DMs; post in channels the bot hasn't joined; resolve channel names; look up the RM by email."]],
      [20, 70, 87])
P("<b>Isolation:</b> a token is only ever loaded with the verified subject of the call's JWT; state binds the flow to the initiating user; tests cover callback state validation, single use, wrong-service state, wrong-user storage, missing-permission rejection, redirect + resume ticket, and per-user encryption.")

H1("7. Streamlit Frontend Design")
B(["<b>Structure:</b> sidebar = identity + <b>Integrations &amp; OAuth</b> cards (connected / disconnected / token expired, account email or workspace, Connect/Reconnect/Disconnect) and preferences; tabs = 💬 Chat · 📁 Documents · 🧾 Activity log · 🧑‍💼 Approvals · 📬 Mock outbox (demo mode only) · 📊 Monitoring and ⚙️ Integrations setup (officers).",
   "<b>State:</b> only the session JWT and user dict live in <font face='Courier'>st.session_state</font> (per browser session, never shared); all data is fetched from the API on each rerun. "
   "A refresh would otherwise log the user out (Streamlit keeps state per websocket), so the JWT is also kept in a <font face='Courier'>SameSite=Strict</font> (Secure on https) cookie that is validated with <font face='Courier'>/auth/me</font> on load and deleted on sign-out; "
   "Streamlit cannot set HttpOnly cookies, an accepted trade-off noted in Section 11. OAuth redirects land on <font face='Courier'>/?oauth=…&amp;status=…&amp;resume=…</font>; <font face='Courier'>handle_oauth_return</font> exchanges the resume ticket (<font face='Courier'>POST /auth/resume</font>, single-use), shows success/error, and the card fragment refreshes.",
   "<b>Chat:</b> history with clear speaker attribution, live per-agent status ('analytics_agent — computed exactly over 931 rows'), token streaming, agent trace + MCP actions in an expander. <b>Aggregates render as metric cards + dataframe + bar chart</b> with a 'How this was computed' expander (periods, plan, repairs); a 'Sources' caption lists file / sheet / rows; 'Clear this conversation' deletes history.",
   "<b>Honest status:</b> a development switch (<font face='Courier'>MCP_MOCK</font>) can simulate the three services locally; the cards then read <i>🧪 simulated — no real account connected</i>, never a green 'connected'. It is off in the submission. <b>Files:</b> widget restricted to xlsx/csv, upload progress per file, list with upload time, status, row/chunk counts and error, delete. <b>Errors:</b> every API call goes through <font face='Courier'>ApiError</font> → friendly message (no stack traces); 409 when a conversation is still processing."])

H1("8. Tech Stack Justification")
table([["Technology", "Role · why chosen over alternatives", "Limits / configuration"],
       ["LLM: pluggable (Ollama qwen2.5:14b local; OpenAI-compatible e.g. Groq; Anthropic)", "No vendor lock-in and customer text can stay on-prem (Ollama) for development; hosted models for the demo. LangChain chat-model interface + structured output. Chosen over a single hard-wired API.", "CPU inference is slow (minutes). Small models need the repair layer. <font face='Courier'>LLM_PROVIDER/LLM_MODEL/…</font>"],
       ["Embeddings: fastembed bge-small + BM25", "Local ONNX, no per-token cost, no data egress; hybrid for exact tokens. Over OpenAI embeddings (egress, cost) and over dense-only (misses '750').", "Small model = lower recall on paraphrase; <font face='Courier'>EMBED_*</font>"],
       ["Qdrant", "Native dense+sparse hybrid with RRF and payload filters (tenant scoping in the store). Over pgvector (no sparse/RRF) and FAISS (no filters/persistence).", "Payloads are plaintext at rest — use disk encryption; single node here"],
       ["LangGraph 0.4 + Postgres checkpointer", "Conditional edges, parallel <font face='Courier'>Send</font>, durable <font face='Courier'>interrupt()</font> for HITL. Over a plain LangChain chain (no branching/persistence) and CrewAI/AutoGen (less control of state & failure paths).", "Checkpoints grow; prune policy not implemented"],
       ["MCP python SDK (FastMCP, streamable HTTP, stateless)", "Standard tool protocol, per-request headers carry the user JWT, scale-out friendly. Over in-process SDK wrappers (no isolation/reuse) and stdio (no per-user headers).", "HTTP overhead per call; <font face='Courier'>MCP_*_URL</font>"],
       ["FastAPI (async) + SQLAlchemy/asyncpg, redis.asyncio, httpx", "No blocking calls in handlers (bcrypt/pandas run in threads); OpenAPI docs free. Over Flask/Django (sync-first).", "—"],
       ["arq (Redis)", "Async-native worker; same Redis as locks/state. Over Celery (heavier, sync-oriented) and RQ (sync).", "At-least-once; jobs are idempotent"],
       ["Streamlit", "Required; fast to build, fragments for polling. ", "Reruns on every interaction; no native cookies (handled with resume tickets)"],
       ["OAuth2: hand-written auth-code + PKCE (httpx)", "~150 lines, full control to host the flow on the MCP servers, scope verification, revocation, runtime-configurable client. Over Authlib/oauthlib (hide the exact steps the assessment wants explained).", "Only Google + Slack providers"],
       ["Token + client-secret storage: Fernet + HKDF in Postgres", "Authenticated encryption; a separate derived key per (user, service) for tokens and a separate context for the app client secret and for uploaded files; no extra service. Over a KMS/Vault (better for production key rotation).", "Master key in env; rotating it forces reconnect"],
       ["Docker Compose", "One command, 9 services, healthchecks. Over Kubernetes (overkill for the scope).", "Single host"]],
      [37, 92, 48], font=7.5)

H1("9. Multi-User Architecture")
B(["Each conversation is its own LangGraph thread keyed <font face='Courier'>user_id:conversation_id</font>, built server-side from the authenticated JWT; every conversation/document/approval endpoint returns <b>404</b> for objects the caller doesn't own. State carries <font face='Courier'>user_id</font> from the session, so no node can act for another user.",
   "A Redis lock allows one in-flight run per thread; asyncio + arq give concurrency across users; <font face='Courier'>test_concurrent_users_do_not_leak</font> runs two users in parallel and asserts no cross-over in retrieval scope, MCP calls or answers. Vector queries are filtered by owner in Qdrant; aggregates and profile reads load only the caller's own (+ bank-wide) files.",
   "OAuth tokens: per-(user, service) rows, per-user derived keys, MCP verifies the JWT subject; contextvars attribute LLM/MCP audit rows to the right user."])

H1("10. Banking Domain Considerations")
B(["<b>Sensitivity:</b> uploaded files and OAuth tokens encrypted at rest; local embedding/LLM option keeps data on-prem; secrets only in env; tokens never in browser, logs or audit payloads (MCP args logged by key only; LLM prompt/response truncated).",
   "<b>No hallucinated numbers:</b> grounded prompts with citations, exact pandas for aggregates and profiles, explicit 'not found'. <b>Regulatory:</b> large-transfer threshold read from the compliance CSV; high-risk requests need officer approval (four-eyes); the assistant never moves money.",
   "<b>Compliance logging:</b> append-only <font face='Courier'>audit_events</font> for files, agent steps, MCP calls, LLM calls, OAuth events, approvals, queryable by kind via <font face='Courier'>/activity</font> and summarised in the Monitoring tab; an audit summary is mirrored to Slack. <b>Prompt-injection:</b> email/Slack/file text is treated as data; tool choice is rule-based; Slack control sequences escaped."])

H1("11. Tradeoffs Made Deliberately & What I Would Do With More Time")
H2("Deliberate tradeoffs")
B(["<b>Rule-based tool selection</b> instead of free LLM tool-calling: less flexible, but auditable and immune to injected instructions.",
   "<b>Exact aggregation over embeddings:</b> one more LLM call and a schema-bound plan language, but correct totals. Not built: free-form SQL generation (a validated plan DSL is safer than executing generated code).",
   "<b>Hand-rolled OAuth, Fernet+env master key, single-host Compose</b>: simple and explainable; no KMS, no HA, no schema migrations (<font face='Courier'>create_all</font>), the session cookie is readable by page scripts (no HttpOnly without a reverse proxy in front of Streamlit), no rate limiting beyond login throttling.",
   "<b>Data scope:</b> files are scoped per uploader (customer or bank-wide), not per <font face='Courier'>customer_id</font> row: an uploaded multi-customer transaction file is visible to its uploader in full."])
H2("With more time")
B(["Map each login to a <font face='Courier'>customer_id</font> and enforce row-level security inside the aggregation engine; a KMS-backed key hierarchy with rotation; Alembic migrations; Qdrant payload encryption.",
   "Evaluation harness with a labelled query set (accuracy of plans/aggregates per model), plan caching for repeated questions, streaming partial tables, a small LLM for planning and a larger one for prose, and OpenTelemetry traces into the monitoring page.",
   "Token-expiry proactive refresh job, multi-workspace Slack, CI pipeline with a live OAuth contract test against provider sandboxes."])

doc = SimpleDocTemplate(OUT, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
                        title="Banking Multi-Agent RAG Chatbot - Design Document", author="Yash Vachhani")


def footer(canvas, d):
    canvas.saveState()
    canvas.setFont("Helvetica", 6.8)
    canvas.setFillColor(GREY)
    canvas.drawString(16 * mm, 7 * mm, "Banking Multi-Agent RAG Chatbot — Design Document")
    canvas.drawRightString(A4[0] - 16 * mm, 7 * mm, f"Page {d.page}")
    canvas.restoreState()


doc.build(story, onFirstPage=footer, onLaterPages=footer)
print("built", OUT)
