"""Optional Tier-3 semantic memory (plan §3.5): fastembed ONNX embeddings
stored as BLOBs in SQLite, cosine similarity via numpy.

Only add if keyword/markdown search stops being enough; skipped initially.
"""


class VectorMemory:
    """Embeds and searches memory entries with numpy cosine similarity."""

    def upsert(self, text: str, metadata: dict) -> None:
        raise NotImplementedError("Phase 7 (optional)")

    def search(self, query: str, top_k: int) -> list[dict]:
        raise NotImplementedError("Phase 7 (optional)")
