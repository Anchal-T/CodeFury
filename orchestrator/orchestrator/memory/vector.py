"""Optional Tier-3 semantic memory (plan §3.5): fastembed ONNX embeddings
stored as BLOBs in SQLite, cosine similarity via numpy.

Markdown knowledge stays the primary (cheapest-first) tier; this module only
supplements it. The embedding model is heavy (~90MB) and loads lazily on the
first ``embed()`` call, so constructing a VectorMemory is free and idle RAM
is unchanged until it is actually used.
"""

from __future__ import annotations

import numpy as np

#: All embeddings are stored as little-endian float32 BLOBs.
_DTYPE = np.float32


def _to_blob(vector: np.ndarray) -> bytes:
    """float32 vector → SQLite BLOB bytes."""
    return np.asarray(vector, dtype=_DTYPE).tobytes()


def _from_blob(blob: bytes) -> np.ndarray:
    """SQLite BLOB bytes → float32 numpy vector."""
    return np.frombuffer(blob, dtype=_DTYPE)


def cosine_top_k(
    query: np.ndarray, matrix: np.ndarray, *, top_k: int
) -> tuple[np.ndarray, np.ndarray]:
    """Rank matrix rows against the query by cosine similarity.

    Returns ``(scores, indices)``, best first. Zero-norm rows (empty texts)
    score 0.0 instead of producing NaN; fewer rows than ``top_k`` returns all.
    """
    if matrix.shape[0] == 0:
        return np.empty(0, dtype=_DTYPE), np.empty(0, dtype=np.int64)
    q = np.asarray(query, dtype=_DTYPE)
    q_norm = np.linalg.norm(q)
    norms = np.linalg.norm(matrix, axis=1)
    safe_q_norm = q_norm if q_norm > 0 else 1.0
    safe_norms = np.where(norms > 0, norms, 1.0)
    scores = (matrix @ q) / (safe_norms * safe_q_norm)
    scores = np.where(norms > 0, scores, 0.0).astype(_DTYPE)
    order = np.argsort(-scores, kind="stable")[:top_k]
    return scores[order], order.astype(np.int64)


class VectorMemory:
    """Embeds and searches memory entries with numpy cosine similarity."""

    def upsert(self, text: str, metadata: dict) -> None:
        raise NotImplementedError("Phase 7")

    def search(self, query: str, top_k: int) -> list[dict]:
        raise NotImplementedError("Phase 7")
