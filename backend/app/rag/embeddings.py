"""Local ONNX embeddings (fastembed): dense for semantics, BM25 sparse for exact
tokens such as '750', 'HL-5Y' or product names. CPU work runs in worker threads so
the event loop is never blocked."""
import asyncio
import threading
from dataclasses import dataclass

from ..config import settings

_lock = threading.Lock()
_dense = None
_sparse = None
_sem = asyncio.Semaphore(2)  # bound concurrent ONNX inference


@dataclass
class Sparse:
    indices: list[int]
    values: list[float]


def _models():
    global _dense, _sparse
    with _lock:
        if _dense is None:
            from fastembed import SparseTextEmbedding, TextEmbedding

            _dense = TextEmbedding(settings.embed_dense_model)
            _sparse = SparseTextEmbedding(settings.embed_sparse_model)
    return _dense, _sparse


def _embed_docs_sync(texts: list[str]) -> tuple[list[list[float]], list[Sparse]]:
    dense, sparse = _models()
    d = [v.tolist() for v in dense.embed(texts)]
    s = [Sparse(e.indices.tolist(), e.values.tolist()) for e in sparse.embed(texts)]
    return d, s


def _embed_query_sync(text: str) -> tuple[list[float], Sparse]:
    dense, sparse = _models()
    d = next(iter(dense.query_embed(text))).tolist()
    e = next(iter(sparse.query_embed(text)))
    return d, Sparse(e.indices.tolist(), e.values.tolist())


async def embed_documents(texts: list[str]) -> tuple[list[list[float]], list[Sparse]]:
    async with _sem:
        return await asyncio.to_thread(_embed_docs_sync, texts)


async def embed_query(text: str) -> tuple[list[float], Sparse]:
    async with _sem:
        return await asyncio.to_thread(_embed_query_sync, text)
