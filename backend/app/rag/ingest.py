"""Ingestion: file -> tables -> chunks -> dense+sparse embeddings -> Qdrant."""
import asyncio
import logging
import uuid
from pathlib import Path

from sqlalchemy import select

from ..audit import log_event
from ..db import SessionLocal
from ..models import Document
from . import store
from .chunking import build_chunks
from .embeddings import embed_documents
from .tables import classify_table, load_tables

log = logging.getLogger(__name__)
BATCH = 64


async def _set_status(doc_id: uuid.UUID, **fields) -> Document | None:
    async with SessionLocal() as s:
        doc = (await s.execute(select(Document).where(Document.id == doc_id))).scalar_one_or_none()
        if doc:
            for k, v in fields.items():
                setattr(doc, k, v)
            await s.commit()
        return doc


async def ingest_document(doc_id: str) -> dict:
    did = uuid.UUID(doc_id)
    doc = await _set_status(did, status="processing", error=None)
    if doc is None:
        return {"ok": False, "error": "document not found"}
    try:
        tables = await asyncio.to_thread(load_tables, Path(doc.storage_path), doc.ext)
        await store.delete_document(doc_id)  # idempotent re-ingest
        n_chunks, n_rows, kinds, n = 0, 0, [], 0
        for sheet, df in tables:
            table_kind = classify_table(df)
            kinds.append(table_kind)
            n_rows += len(df)
            chunks = build_chunks(doc.filename, sheet, table_kind, df)
            for i in range(0, len(chunks), BATCH):
                batch = chunks[i : i + BATCH]
                dense, sparse = await embed_documents([c.text for c in batch])
                points = []
                for c, d, s in zip(batch, dense, sparse):
                    payload = {
                        "text": c.text,
                        "kind": c.kind,
                        "table_kind": table_kind,
                        "sheet": c.sheet,
                        "filename": doc.filename,
                        "scope": doc.scope,
                        "owner_id": str(doc.owner_id) if doc.owner_id else "",
                        "row_start": c.row_start,
                        "row_end": c.row_end,
                        "data": c.data,
                    }
                    points.append(store.make_point(doc_id, n, d, s, payload))
                    n += 1
                await store.upsert(points)
            n_chunks += len(chunks)
        await _set_status(did, status="ready", kinds=sorted(set(kinds)), n_rows=n_rows, n_chunks=n_chunks)
        await log_event("file_indexed", user_id=str(doc.owner_id) if doc.owner_id else None, filename=doc.filename, rows=n_rows, chunks=n_chunks, kinds=sorted(set(kinds)))
        return {"ok": True, "chunks": n_chunks}
    except Exception as exc:
        log.exception("ingestion failed for %s", doc_id)
        await _set_status(did, status="failed", error=str(exc)[:500])
        await log_event("file_failed", user_id=str(doc.owner_id) if doc.owner_id else None, filename=doc.filename, error=str(exc)[:300])
        return {"ok": False, "error": str(exc)}
