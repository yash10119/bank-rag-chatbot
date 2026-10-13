# Multi-Agent Banking RAG Chatbot (LangGraph + MCP + Excel/CSV RAG)

A customer-service assistant for a retail bank. It answers from **Excel and CSV files only**, routes
a message through specialised LangGraph agents, and performs real workflows (calendar booking,
email, Slack) through **MCP servers using each user's own OAuth2 connection**.

```
docker compose up --build        # then open http://localhost:8501
```

## Architecture

```
Streamlit (8501) ──HTTP/SSE──▶ FastAPI (8000, fully async) ──▶ LangGraph (Postgres checkpointer)
                                  │  │                              │
                                  │  └─ arq jobs ─▶ Redis ─▶ worker │  MCP client (per-call user token)
                                  │                                 ▼
                        Qdrant (dense + BM25) ◀── ingestion    mcp-gmail · mcp-calendar · mcp-slack
                        Postgres (users, encrypted tokens, audit, approvals, checkpoints)
```

| Container | Role |
|---|---|
| `frontend` | Streamlit: auth, OAuth connect buttons, chat with live agent activity, file upload, activity log, officer approvals |
| `api` | FastAPI + LangGraph run; SSE streaming; all I/O is `async` (asyncpg, redis.asyncio, AsyncQdrantClient, httpx, psycopg async pool) |
| `worker` | arq agent worker: document ingestion/embedding, **resuming paused graphs after officer approval**, audit mirroring to Slack |
| `mcp-gmail`, `mcp-calendar`, `mcp-slack` | MCP servers (streamable HTTP) wrapping the Google/Slack APIs |
| `redis` | broker, single-use OAuth state, per-conversation locks, rate limits |
| `qdrant` | vector store |
| `postgres` | app data + LangGraph checkpoints |

### The graph (`backend/app/agents/graph.py`)

```
intake ─(ambiguous)─▶ clarify ─▶ intake                     HITL #1: the customer
   │
risk_gate ─(out of scope)─▶ respond
   ├─(high risk)─▶ request_approval ─▶ await_approval       HITL #2: a bank officer
   │                                      ├ rejected ─▶ respond
   ▼                                      ▼ approved
dispatch ══Send fan-out (parallel)══▶ rates_agent │ eligibility_agent │ transaction_agent │ inbox_agent │ faq_agent
                                                  ▼
                               scheduler  (calendar availability + double-booking guard)
                                                  ▼
                               notify     (Gmail customer/RM, Slack RM/#loans-team/#compliance-alerts — in parallel)
                                                  ▼
                               respond (token-streamed) ─▶ audit ─▶ END
```

The path depends on the message. The user story ("5-year home loan rate for credit score above 750 **and** schedule a call
with my RM this Thursday") produces: `intake` extracts two intents → `rates_agent` retrieves the matching row from the
rate CSV while `eligibility_agent` checks the customer's profile Excel in parallel → `scheduler` finds a free slot on
Thursday and creates the Google Calendar invite → `notify` emails the confirmation, DMs the RM on Slack → one streamed answer.

### RAG pipeline (`backend/app/rag/`) — `.xlsx` / `.csv` only

* **Ingress**: extension *and* content signature checked (a renamed PDF/zip is rejected); size/row limits; server-generated storage names.
* **Chunking** is table-aware: a *schema* chunk per sheet (columns, numeric ranges, categorical values), *row* chunks
  (`file > sheet] col: value; …`, grouped only for very large tables), and *monthly summary* chunks for transaction tables.
  Tables are auto-classified (`rates`, `fees`, `compliance`, `eligibility_rules`, `profile`, `transactions`).
* **Embeddings**: local ONNX (fastembed) — dense `bge-small` + sparse BM25 — so exact tokens like `750` or `Home Loan` match.
* **Retrieval**: Qdrant hybrid query with RRF fusion. **Tenant scoping lives in the store**: every query is filtered to
  `scope=bank OR (scope=customer AND owner_id=<caller>)`; callers can't widen it.
* **Exact reads**: profile fields and transaction analytics (totals, large-transfer / outlier / rapid-succession / repeated-amount
  flags) are computed with pandas over the user's complete files — numbers are never left to an LLM to approximate.
* The large-transfer threshold is read from the bank's compliance CSV (falls back to `LARGE_TRANSFER_THRESHOLD`).

### Multi-user isolation & OAuth

* Each user connects **their own** Gmail, Calendar and Slack from the Streamlit sidebar. **The MCP servers host the OAuth flow**
  (`/oauth/start`, `/oauth/callback`): the API only signs a 5-minute connect ticket for the logged-in user, the browser goes to the MCP
  server, then the provider (authorization-code + PKCE for Google, single-use `state` bound to the user and service). The app's client ID/secret
  identify the *app*; every user's token is separate. There is no shared service credential. Granted scopes are verified, and disconnecting revokes the token at the provider.
* Tokens are stored as Fernet ciphertext with a key **derived per (user, service)** via HKDF from `TOKEN_ENCRYPTION_KEY`
  — a blob copied to another user's row cannot be decrypted.
* The API calls MCP servers with a 60-second JWT (`sub=user_id`, `aud=mcp:<service>`) built from the **graph state's user id** (set from
  the authenticated session, never from model output). MCP tools take no user/token arguments and load only the verified subject's token.
* Each conversation is its own LangGraph thread `<user_id>:<conversation_id>`; a Redis lock allows one in-flight run per thread.
  Conversation/document/approval endpoints return 404 for objects owned by someone else.
* Slack content is escaped (`<!channel>` etc. can't be injected); email/Slack text from third parties is treated as data in prompts.

### Human-in-the-loop

1. **Ambiguity** (e.g. a transfer without an amount): `clarify` calls `interrupt()`; the question appears in chat and the customer's next message resumes the graph.
2. **High risk** (transfer ≥ compliance threshold, fraud report, large disputed amount): `request_approval` stores an approval and alerts
   `#compliance-alerts`; `await_approval` pauses the graph durably in the Postgres checkpointer. An **officer** approves/rejects in the
   *Approvals* tab (four-eyes: officers can't decide their own requests). The API enqueues a worker job that resumes the thread,
   runs the remaining nodes, and posts the outcome into the customer's conversation. The assistant never claims to move money itself.

## Setup

1. `cp .env.example .env` and fill in the three secrets (the LLM runs locally via Ollama: install it, then `ollama pull llama3.1:8b`; set `LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY` to use Claude instead), and the OAuth credentials below.
2. **Google** (Gmail + Calendar): Google Cloud Console → enable *Gmail API* and *Google Calendar API* → OAuth consent screen
   (add every user as a test user) → Credentials → *OAuth client ID (Web)* → register **both** redirect URIs
   `http://localhost:8101/oauth/callback` (Gmail) and `http://localhost:8102/oauth/callback` (Calendar).
3. **Slack**: api.slack.com/apps → create app → OAuth & Permissions → bot scopes `chat:write chat:write.public channels:read users:read users:read.email`
   → redirect URL. Slack needs **HTTPS**, so tunnel the Slack MCP server (`ngrok http 8103`) and set `SLACK_REDIRECT_URI=https://<tunnel>/oauth/callback`.
   Create the channels you route to (`#loans-team`, `#compliance-alerts`, `#audit-log`, `#rm-notifications`; names are editable in *Preferences*).
4. `docker compose up --build`. First boot seeds sample bank files (rates, eligibility rules, fee schedule, compliance thresholds) and indexes them.
5. Open http://localhost:8501 → create an account (emails listed in `OFFICER_EMAILS` become officers) → *Documents → Load demo customer data*
   → connect Gmail/Calendar/Slack in the sidebar → chat.

Try: *"What is the interest rate on a 5-year home loan for a credit score above 750, and can you schedule a call with my relationship manager this Thursday?"*
and *"I want to transfer 50,000 to a new beneficiary"* (triggers officer approval — register a second account listed in `OFFICER_EMAILS` to approve).

## Mock MCP mode (development only)

The default is `MCP_MOCK=false`: real Gmail, Calendar and Slack through each user's own OAuth connection.
`MCP_MOCK=true` is an offline development switch that keeps the same three MCP servers and tool signatures but
stores emails, calendar events and Slack messages in Postgres (`mock_items`) instead of calling Google/Slack.
The sidebar then shows the services as **🧪 simulated**, never as connected, and the output is browsable in the
**📬 Mock outbox** tab. It is not used in the deployed demo.

## OAuth client configured in the app (no static credentials)

The Google / Slack **client ID and secret are not kept in `.env` or code**. A bank officer (an email listed in `OFFICER_EMAILS`) opens
**⚙️ Integrations setup**, sees the exact redirect URIs and scopes to register, and pastes the client there. It is stored as Fernet ciphertext in
`oauth_clients`, is never returned to the browser (the ID is masked, the secret never shown), can be replaced or removed without a restart, and
every change is audited. `.env` values remain only as a fallback. The client identifies the *application*; each user still signs in with their
own account and receives their own token. Google has no client-less flow and no dynamic client registration, so one client must exist.

## Aggregated queries (structured compute, not top-K retrieval)

Questions such as *"total debits April–June, month-wise"*, *"how many transactions above Rs. 10,000 last quarter"*, *"top 3 categories for
the last 3 months"*, *"which branch disbursed the most in Q1 and how does that compare with Q4"* cannot be answered from the K most similar
rows. They are routed to `analytics_agent`:

1. **Detect.** The intake LLM classifies the intent `analytics_query`; a regex backstop (aggregate words + a parsed period) catches phrasing small models miss.
2. **Resolve dates in code** (`rag/periods.py`): `Q1 2026`, `April to June`, `last quarter`, `first half`, `last 3 months`, `this financial year` (FY start month = `FINANCIAL_YEAR_START_MONTH`). The model never writes dates.
3. **Plan.** The LLM fills a small typed `AggPlan` (table, filters, group-by, metrics, optional join / denominator / comparison) for the 4 most relevant tables. It never sees rows.
4. **Repair + validate** (`repair_plan`, `resolve_column/table`): predictable model mistakes are fixed from the question text and real schema (missing join, prose in a numeric filter, counting instead of summing, identifier group-by ...); unknown columns/tables are rejected, never guessed. One retry with the error fed back.
5. **Execute** with pandas over **every** row of the full files (`rag/aggregate.py`): period filter, filters, group-by (`month|quarter|year` derived), sum/count/count_distinct/avg/min/max, mean-of-groups, share-of-total, top-N, quarter-on-quarter comparison, cross-file join and cross-file ratio (`denominator`).
6. **Answer.** Metric cards, table, chart and a narrative **built from the computed numbers** (no LLM rewrite, so no hallucinated figure). Empty matches, periods outside the data's date coverage and unfiltered statuses are reported explicitly.

## Audit & monitoring

Every file upload/rejection/indexing, agent turn (all steps), MCP tool call (service, tool, outcome, latency), LLM prompt and response, OAuth event
(connect/refresh/fail/disconnect) and approval decision is one row in `audit_events` (JSON payload, indexed by kind and time, no tokens).
Query it with `GET /activity?kind=mcp_tool_call,llm_call&limit=200` or in the **🧾 Activity log** tab; officers also get **📊 Monitoring**
(`GET /monitor`: active runs, queue depth, MCP success per tool, LLM latency, recent tool calls). Uploaded files are encrypted at rest (Fernet).

## Example API calls

```bash
API=http://localhost:8000
TOKEN=$(curl -s $API/auth/login -H 'content-type: application/json' -d '{"email":"me@example.com","password":"..."}' | jq -r .token)

# upload (a .pdf / .md / .docx / image is rejected with HTTP 415)
curl -s $API/documents -H "Authorization: Bearer $TOKEN" -F file=@Dataset/csv/01b_Transactions.csv -F scope=customer
curl -s $API/documents -H "Authorization: Bearer $TOKEN" -F file=@notes.pdf            # -> 415 "Only .xlsx and .csv files are accepted."

# chat (server-sent events: step | token | final)
CID=$(curl -s -X POST $API/conversations -H "Authorization: Bearer $TOKEN" | jq -r .id)
curl -N $API/conversations/$CID/messages -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
     -d '{"text":"Total debits and credits between April and June, month-wise"}'

curl -s $API/oauth/services -H "Authorization: Bearer $TOKEN"       # connected / expired / disconnected per service
curl -s "$API/activity?kind=mcp_tool_call" -H "Authorization: Bearer $TOKEN"
```
Interactive docs: `http://localhost:8000/docs` (Swagger) and `/redoc`.

## Environment variables

All variables are listed with comments in `.env.example`. Key groups: `POSTGRES_*`; `LLM_PROVIDER` (`ollama` | `openai` = any OpenAI-compatible API such as Groq/OpenRouter | `anthropic`),
`LLM_MODEL`, `OLLAMA_BASE_URL`, `OPENAI_API_KEY`/`OPENAI_BASE_URL`, `ANTHROPIC_API_KEY`; secrets `JWT_SECRET`, `TOKEN_ENCRYPTION_KEY` (token + file encryption master key),
`MCP_INTERNAL_SECRET`; `MCP_MOCK`; OAuth `GOOGLE_CLIENT_ID/SECRET`, `MCP_PUBLIC_GMAIL/CALENDAR/SLACK` (redirect bases), `SLACK_CLIENT_ID/SECRET`, `SLACK_REDIRECT_URI`;
`PUBLIC_API_URL`, `FRONTEND_URL`; `OFFICER_EMAILS`, `FINANCIAL_YEAR_START_MONTH`, `LARGE_TRANSFER_THRESHOLD`, `DEFAULT_TIMEZONE`.

## Make targets

`make start` (docker compose up --build -d) · `make stop` · `make test` (pytest, no network/LLM needed) · `make lint` (ruff) · `make logs`.

## Notes & honest limits

* Slack: the "internal team" channels are posted to using the **initiating user's** Slack connection (per the per-user-credentials requirement);
  in a real deployment each staff member/workspace connects their own.
* Calendar availability of the RM is only readable if the RM's calendar is shared with the customer's Google account; otherwise the
  system says so, and double-booking of the RM across customers is still prevented by a DB-level reservation under a per-RM advisory lock.
* Schema is created with `create_all` on startup (no migrations). Login persists for the browser session only (no cookie).
* Tests: `cd backend && pip install -r requirements.txt pytest pytest-asyncio && pytest` (no network or Docker needed).
