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
    """Embeds and searches memory entries with numpy cosine similarity.

    Entries live in StateStore's ``vector_entries`` table (same SQLite file,
    BLOB embeddings); the embedder is injectable — tests use deterministic
    fakes, production uses a lazily-loaded fastembed ONNX model.
    """

    def __init__(self, store, embedder: "FastEmbedder | None" = None) -> None:
        self.store = store
        self.embedder = embedder if embedder is not None else FastEmbedder()

    def upsert(self, text: str, *, source: str, ref: str, title: str = "") -> None:
        """Embed one entry and store it; (source, ref) replaces on re-run."""
        vector = np.asarray(self.embedder.embed([text])[0], dtype=_DTYPE)
        self.store.upsert_vector_entry(
            source=source,
            ref=ref,
            title=title,
            body=text,
            dim=int(vector.shape[0]),
            embedding=_to_blob(vector),
        )

    def search(self, query: str, *, top_k: int = 5) -> list[dict]:
        """Top-k most similar entries as dicts with score + metadata."""
        query_vec = np.asarray(self.embedder.embed([query])[0], dtype=_DTYPE)
        rows = self.store.vector_rows(dim=int(query_vec.shape[0]))
        if not rows:
            return []
        matrix = np.stack([_from_blob(row.embedding) for row in rows])
        scores, indices = cosine_top_k(query_vec, matrix, top_k=top_k)
        hits: list[dict] = []
        for score, index in zip(scores.tolist(), indices.tolist()):
            row = rows[index]
            hits.append(
                {
                    "score": float(score),
                    "source": row.source,
                    "ref": row.ref,
                    "title": row.title,
                    "body": row.body,
                }
            )
        return hits


class FastEmbedder:
    """fastembed-backed embedder; loads its ONNX model on first use only.

    The import is deferred so importing this module never requires fastembed
    to be installed — Tier-3 stays optional (plan §3.5).
    """

    DEFAULT_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name or self.DEFAULT_MODEL_NAME
        self._model = None

    def _load(self):
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=self._model_name)
        return self._model

    def embed(self, texts: list[str]) -> list[np.ndarray]:
        if not texts:
            return []
        model = self._load()
        return [np.asarray(vec, dtype=_DTYPE) for vec in model.embed(texts)]


#: Operator/test kill-switch: ``0`` forces Tier-3 off even when fastembed is
#: installed (keeps test suites hermetic and gives ops a clean escape hatch).
_SEMANTIC_ENV_VAR = "ORCHESTRATOR_SEMANTIC"

#: Cap on each recalled entry's length inside a planning-context block.
_SNIPPET_MAX_CHARS = 200


def auto_memory(store) -> "VectorMemory | None":
    """Construct a VectorMemory iff Tier-3 is available and not switched off.

    Returns None (silently) when fastembed is missing or
    ORCHESTRATOR_SEMANTIC=0 — callers treat None as 'no vectors', keeping the
    run fully functional on markdown alone (plan §3.5 cheapest-first).
    """
    import os

    if os.environ.get(_SEMANTIC_ENV_VAR) == "0":
        return None
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return None
    return VectorMemory(store)


def format_history_block(hits: list[dict]) -> str:
    """Render search hits as an appendable 'Relevant history' block."""
    if not hits:
        return ""
    lines = ["Relevant history (semantic recall, best first):"]
    for hit in hits:
        label = hit.get("title") or hit.get("ref", "")
        snippet = " ".join(str(hit.get("body", "")).split())
        if len(snippet) > _SNIPPET_MAX_CHARS:
            snippet = snippet[: _SNIPPET_MAX_CHARS - 3] + "..."
        lines.append(f"- {label} (score {hit['score']:.2f}): {snippet}")
    return "\n".join(lines)


def recall_context(memory: "VectorMemory | None", goal: str, *, top_k: int = 3) -> str:
    """Top-k relevant history for a planning goal, formatted; '' when none.

    Fail-soft by design: an empty vector store short-circuits before any
    model load, and an embedder failure must never break planning — markdown
    stays primary (plan §3.5).
    """
    if memory is None or not goal.strip():
        return ""
    try:
        if not memory.store.vector_rows():
            return ""
        hits = memory.search(goal, top_k=top_k)
    except Exception:  # noqa: BLE001 — Tier-3 is strictly additive
        return ""
    return format_history_block(hits)
