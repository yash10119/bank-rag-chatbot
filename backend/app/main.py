import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from .agents.runtime import close_runtime, init_runtime
from .config import settings
from .db import SessionLocal, create_tables, engine
from .demo_data import bank_files
from .models import Document
from .rag import store
from .routers import auth, chat, documents, mock, oauth

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bank-api")


async def seed_bank_files() -> None:
    """First boot: ingest sample bank-wide rate/fee/compliance files so the demo works out of the box."""
    async with SessionLocal() as s:
        if (await s.execute(select(Document.id).where(Document.scope == "bank").limit(1))).first():
            return
        for name, content in bank_files().items():
            async with SessionLocal() as s2:
                doc = await documents.save_and_queue(s2, filename=name, content=content, scope="bank", owner_id=None)
                log.info("seeded %s (%s)", name, doc.id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    for attempt in range(30):  # wait for Postgres on cold start
        try:
            await create_tables()
            break
        except Exception:
            if attempt == 29:
                raise
            await asyncio.sleep(2)
    await store.ensure_collection()
    app.state.runtime = await init_runtime()
    await seed_bank_files()
    yield
    await close_runtime()
    await engine.dispose()


app = FastAPI(title="Banking Multi-Agent RAG API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=[settings.frontend_url], allow_credentials=False,
    allow_methods=["*"], allow_headers=["Authorization", "Content-Type"],
)
for r in (auth.router, oauth.router, documents.router, chat.router, mock.router):
    app.include_router(r)


@app.get("/health")
async def health():
    return {"status": "ok"}
