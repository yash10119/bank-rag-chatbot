"""File routes: upload, list, delete and demo data. Uploads are validated (.xlsx / .csv only), encrypted at
rest and handed to the worker for indexing."""
import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import log_event
from ..config import settings
from ..db import get_session
from ..demo_data import customer_files
from ..deps import current_user
from ..models import Document, User
from ..queue import enqueue
from ..rag import store
from ..rag.tables import UnsupportedFile, validate_upload
from ..security import encrypt_bytes

router = APIRouter(prefix="/documents", tags=["documents"])


def out(d: Document) -> dict:
    """Document fields shown in the file list."""
    return {
        "id": str(d.id), "filename": d.filename, "scope": d.scope, "status": d.status, "kinds": d.kinds or [],
        "rows": d.n_rows, "chunks": d.n_chunks, "error": d.error, "size_bytes": d.size_bytes,
        "created_at": d.created_at.isoformat(), "mine": d.owner_id is not None,
    }


async def save_and_queue(session: AsyncSession, *, filename: str, content: bytes, scope: str, owner_id: uuid.UUID | None) -> Document:
    """Validate the file, write it encrypted under a server-generated name, record it and queue ingestion.
    validate_upload raises UnsupportedFile here, which is where non-.xlsx/.csv uploads are rejected.
    """
    ext = validate_upload(filename, content)
    doc_id = uuid.uuid4()
    path = Path(settings.upload_dir) / f"{doc_id}{ext}"  # server-generated name: no path traversal
    await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_bytes, encrypt_bytes(content))  # encrypted at rest (Fernet)
    doc = Document(id=doc_id, owner_id=owner_id, scope=scope, filename=Path(filename).name[:300], ext=ext,
                   size_bytes=len(content), storage_path=str(path))
    session.add(doc)
    await session.commit()
    await enqueue("ingest_document_job", str(doc_id))
    return doc


@router.post("")
async def upload(
    file: UploadFile = File(...),
    scope: str = Form("customer"),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """Upload one file for indexing. Reads in bounded blocks to enforce MAX_UPLOAD_MB; only officers may
    upload bank-wide files; anything that is not a valid .xlsx or .csv gets 415 with a clear reason.
    """
    if scope not in ("customer", "bank"):
        raise HTTPException(422, "scope must be 'customer' or 'bank'")
    if scope == "bank" and user.role != "officer":
        raise HTTPException(403, "Only bank officers can upload bank-wide files")
    limit = settings.max_upload_mb * 1024 * 1024
    chunks, size = [], 0
    while block := await file.read(1024 * 1024):
        size += len(block)
        if size > limit:
            raise HTTPException(413, f"File exceeds {settings.max_upload_mb} MB")
        chunks.append(block)
    try:
        doc = await save_and_queue(
            session, filename=file.filename or "upload", content=b"".join(chunks), scope=scope,
            owner_id=user.id if scope == "customer" else None,
        )
    except UnsupportedFile as exc:
        await log_event("file_rejected", user_id=str(user.id), filename=(file.filename or "")[:200], reason=str(exc))
        raise HTTPException(415, str(exc)) from None
    await log_event("file_uploaded", user_id=str(user.id), filename=doc.filename, scope=scope, size_bytes=doc.size_bytes)
    return out(doc)


@router.get("")
async def list_documents(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """Files visible to the caller: shared bank files and the caller's own uploads."""
    rows = (
        await session.execute(
            select(Document)
            .where(or_(Document.scope == "bank", Document.owner_id == user.id))
            .order_by(Document.created_at.desc())
        )
    ).scalars()
    return [out(d) for d in rows]


@router.post("/demo")
async def load_demo(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """Create and index the sample customer profile and transaction history for the caller."""
    docs = []
    for name, content in customer_files(user.name, user.email).items():
        docs.append(out(await save_and_queue(session, filename=name, content=content, scope="customer", owner_id=user.id)))
    return docs


@router.delete("/{doc_id}")
async def delete(doc_id: uuid.UUID, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """Remove a file, its stored bytes and its index entries. Owners delete their own files; officers delete bank files."""
    doc = await session.get(Document, doc_id)
    allowed = doc and ((doc.owner_id == user.id) or (doc.scope == "bank" and user.role == "officer"))
    if not allowed:
        raise HTTPException(404, "Document not found")  # same answer for 'not yours' and 'missing'
    await store.delete_document(str(doc.id))
    await asyncio.to_thread(Path(doc.storage_path).unlink, missing_ok=True)
    await session.delete(doc)
    await session.commit()
    return {"ok": True}
