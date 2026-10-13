"""Application settings, read once from the environment / .env (see .env.example for every variable)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed settings shared by the API, the worker and the three MCP servers. Field names map to upper-case env vars."""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://bank:bank@localhost:5432/bank"
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"
    upload_dir: str = "./data/uploads"

    llm_provider: str = "ollama"  # "ollama" (local) | "anthropic" | "openai" (OpenAI-compatible)
    llm_model: str = "llama3.1:8b"
    ollama_base_url: str = "http://localhost:11434"
    anthropic_api_key: str = ""
    openai_api_key: str = ""  # LLM_PROVIDER=openai: any OpenAI-compatible API (Groq, OpenRouter, Together ...)
    openai_base_url: str = "https://api.groq.com/openai/v1"

    jwt_secret: str = "dev-jwt-secret-change-me"
    token_encryption_key: str = "dev-encryption-key-change-me"
    mcp_internal_secret: str = "dev-mcp-secret-change-me"

    public_api_url: str = "http://localhost:8000"
    frontend_url: str = "http://localhost:8501"

    google_client_id: str = ""
    google_client_secret: str = ""
    # Each MCP server hosts its own OAuth endpoints; these are the browser-reachable bases.
    # Google redirect URIs to register: <mcp_public_gmail>/oauth/callback and <mcp_public_calendar>/oauth/callback
    mcp_public_gmail: str = "http://localhost:8101"
    mcp_public_calendar: str = "http://localhost:8102"
    mcp_public_slack: str = "http://localhost:8103"
    slack_client_id: str = ""
    slack_client_secret: str = ""
    slack_redirect_uri: str = ""  # HTTPS tunnel to the Slack MCP server: https://<tunnel>/oauth/callback

    mcp_gmail_url: str = "http://localhost:8101/mcp"
    mcp_calendar_url: str = "http://localhost:8102/mcp"
    mcp_slack_url: str = "http://localhost:8103/mcp"
    mcp_port: int = 8100
    # Demo mode: the Gmail/Calendar/Slack MCP servers keep emails, events and messages in
    # Postgres instead of calling Google/Slack, and every user counts as "connected".
    mcp_mock: bool = False

    officer_emails: str = ""
    large_transfer_threshold: float = 10000.0
    default_timezone: str = "Asia/Kolkata"
    financial_year_start_month: int = 4  # India: April-March
    branch_address: str = "Main Branch, 1 Banking Plaza, Mumbai 400001"
    bank_name: str = "Demo Retail Bank"
    demo_rm_name: str = "Priya Sharma"
    demo_rm_email: str = "rm@example.com"

    max_upload_mb: int = 25
    max_table_rows: int = 100_000

    embed_dense_model: str = "BAAI/bge-small-en-v1.5"
    embed_dense_dim: int = 384
    embed_sparse_model: str = "Qdrant/bm25"
    collection: str = "bank_tables"

    @property
    def psycopg_url(self) -> str:
        """Same database in plain psycopg form, which the LangGraph Postgres checkpointer requires."""
        return self.database_url.replace("+asyncpg", "")

    @property
    def officer_email_set(self) -> set[str]:
        """Emails that receive the officer role at registration, lower-cased for comparison."""
        return {e.strip().lower() for e in self.officer_emails.split(",") if e.strip()}


@lru_cache
def get_settings() -> Settings:
    """Build the settings object once per process."""
    return Settings()


settings = get_settings()
