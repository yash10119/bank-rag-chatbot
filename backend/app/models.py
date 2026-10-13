import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="customer")  # customer | officer
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    # Per-user Slack channel routing, e.g. {"loans": "#loans-team", ...}
    prefs: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class OAuthConnection(Base):
    """One row per (user, service). `enc_blob` is Fernet ciphertext whose key is
    derived from (master key, user_id, service)."""

    __tablename__ = "oauth_connections"
    __table_args__ = (UniqueConstraint("user_id", "service", name="uq_user_service"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    service: Mapped[str] = mapped_column(String(20))  # gmail | calendar | slack
    enc_blob: Mapped[str] = mapped_column(Text)
    scopes: Mapped[str] = mapped_column(Text, default="")
    account_label: Mapped[str] = mapped_column(String(320), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class OAuthClient(Base):
    """The application's OAuth client (ID + secret) per provider, entered in the Settings page by an officer.
    `enc_blob` is Fernet ciphertext; the secret is never sent back to the browser."""

    __tablename__ = "oauth_clients"
    provider: Mapped[str] = mapped_column(String(20), primary_key=True)  # google | slack
    enc_blob: Mapped[str] = mapped_column(Text)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="New conversation")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))  # user | assistant | system
    content: Mapped[str] = mapped_column(Text)
    trace: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # agent steps + actions
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    scope: Mapped[str] = mapped_column(String(20))  # bank (shared) | customer (owner only)
    filename: Mapped[str] = mapped_column(String(300))
    ext: Mapped[str] = mapped_column(String(10))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    storage_path: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|processing|ready|failed
    kinds: Mapped[list] = mapped_column(JSON, default=list)  # table kinds detected per sheet
    n_rows: Mapped[int] = mapped_column(Integer, default=0)
    n_chunks: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Approval(Base):
    """Human-in-the-loop request. The LangGraph thread is paused on an
    interrupt() until an officer decides."""

    __tablename__ = "approvals"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    thread_id: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[str] = mapped_column(Text, default="")
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Appointment(Base):
    __tablename__ = "appointments"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    reference: Mapped[str] = mapped_column(String(40), unique=True)
    rm_email: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    gcal_event_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|confirmed|failed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MockItem(Base):
    """Output of the mock MCP servers (MCP_MOCK=true): sent emails, calendar events, Slack
    messages and the seeded demo inbox. Always scoped to one user."""

    __tablename__ = "mock_items"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    service: Mapped[str] = mapped_column(String(20), index=True)  # gmail | calendar | slack
    kind: Mapped[str] = mapped_column(String(20))  # inbox | sent | event | message | card | dm
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
