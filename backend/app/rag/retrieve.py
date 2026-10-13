from . import store


async def retrieve(
    query: str, user_id: str, *, scope: str = "all", table_kinds: list[str] | None = None, k: int = 8
) -> list[dict]:
    """Hybrid search restricted to what `user_id` may see. Returns payload dicts."""
    return await store.hybrid_search(query, user_id, scope=scope, table_kinds=table_kinds, k=k)


def format_context(hits: list[dict]) -> str:
    """Numbered, citable context block for an LLM prompt."""
    if not hits:
        return "(no matching rows found in the uploaded files)"
    return "\n".join(f"[S{i}] {h['text']}" for i, h in enumerate(hits, 1))


def sources(hits: list[dict]) -> list[dict]:
    return [
        {"ref": f"S{i}", "file": h.get("filename"), "sheet": h.get("sheet"), "rows": [h.get("row_start"), h.get("row_end")]}
        for i, h in enumerate(hits, 1)
    ]
