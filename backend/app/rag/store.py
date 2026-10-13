"""Qdrant vector store with hybrid (dense + BM25, RRF-fused) retrieval.

Tenant isolation is enforced HERE, not by callers: every search is filtered to
(scope == bank) OR (scope == customer AND owner_id == <caller>)."""
import asyncio
import uuid

from qdrant_client import AsyncQdrantClient, models

from ..config import settings
from .embeddings import Sparse, embed_query

_client: AsyncQdrantClient | None = None


def client() -> AsyncQdrantClient:
    global _client
    if _client is None:
        _client = AsyncQdrantClient(url=settings.qdrant_url, timeout=30)
    return _client


async def ensure_collection(retries: int = 30) -> None:
    c = client()
    for attempt in range(retries):
        try:
            existing = {x.name for x in (await c.get_collections()).collections}
            break
        except Exception:
            if attempt == retries - 1:
                raise
            await asyncio.sleep(2)
    if settings.collection not in existing:
        await c.create_collection(
            settings.collection,
            vectors_config={
                "dense": models.VectorParams(size=settings.embed_dense_dim, distance=models.Distance.COSINE)
            },
            sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
        )
    for field in ("scope", "owner_id", "doc_id", "kind", "table_kind"):
        await c.create_payload_index(settings.collection, field, models.PayloadSchemaType.KEYWORD)


def _sv(s: Sparse) -> models.SparseVector:
    return models.SparseVector(indices=s.indices, values=s.values)


def point_id(doc_id: str, n: int) -> str:
    return str(uuid.uuid5(uuid.UUID(doc_id), str(n)))


async def upsert(points: list[models.PointStruct]) -> None:
    await client().upsert(settings.collection, points=points, wait=True)


def make_point(doc_id: str, n: int, dense: list[float], sparse: Sparse, payload: dict) -> models.PointStruct:
    return models.PointStruct(
        id=point_id(doc_id, n), vector={"dense": dense, "bm25": _sv(sparse)}, payload={**payload, "doc_id": doc_id}
    )


async def delete_document(doc_id: str) -> None:
    await client().delete(
        settings.collection,
        points_selector=models.FilterSelector(
            filter=models.Filter(must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))])
        ),
        wait=True,
    )


def scope_filter(user_id: str, scope: str, table_kinds: list[str] | None) -> models.Filter:
    bank = models.FieldCondition(key="scope", match=models.MatchValue(value="bank"))
    mine = models.Filter(
        must=[
            models.FieldCondition(key="scope", match=models.MatchValue(value="customer")),
            models.FieldCondition(key="owner_id", match=models.MatchValue(value=str(user_id))),
        ]
    )
    if scope == "bank":
        visible = models.Filter(should=[bank])
    elif scope == "customer":
        visible = mine
    else:
        visible = models.Filter(should=[bank, mine])
    must: list = [visible]
    if table_kinds:
        must.append(models.FieldCondition(key="table_kind", match=models.MatchAny(any=table_kinds)))
    return models.Filter(must=must)


async def hybrid_search(
    query: str, user_id: str, *, scope: str = "all", table_kinds: list[str] | None = None, k: int = 8
) -> list[dict]:
    dense, sparse = await embed_query(query)
    flt = scope_filter(user_id, scope, table_kinds)
    res = await client().query_points(
        settings.collection,
        prefetch=[
            models.Prefetch(query=dense, using="dense", limit=max(k * 3, 20), filter=flt),
            models.Prefetch(query=_sv(sparse), using="bm25", limit=max(k * 3, 20), filter=flt),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=k,
        with_payload=True,
    )
    return [{"score": p.score, **(p.payload or {})} for p in res.points]
