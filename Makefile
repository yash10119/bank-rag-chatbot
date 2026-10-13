.PHONY: start stop logs test lint fmt ps oauth-check

# Single-command startup: builds and starts API, worker, 3 MCP servers, Streamlit, Postgres, Redis, Qdrant.
start:
	docker compose up --build -d
	@echo "Streamlit: http://localhost:8501   API docs: http://localhost:8000/docs"

stop:
	docker compose down

logs:
	docker compose logs -f api worker

ps:
	docker compose ps

# Unit + graph-flow tests (no network, no Docker, no LLM needed).
test:
	cd backend && python -m pytest -q

# Lint (ruff). Install with: pip install ruff
lint:
	cd backend && ruff check app tests
	cd frontend && ruff check .

fmt:
	cd backend && ruff check --fix app tests

# Checks .env + prints the exact redirect URIs/scopes to register for real Google/Slack OAuth.
oauth-check:
	python scripts/oauth_preflight.py
