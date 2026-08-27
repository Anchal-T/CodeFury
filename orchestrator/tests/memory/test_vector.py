"""Tests for Tier-3 vector math (orchestrator.memory.vector).

Hermetic: fake embeddings only — no model download, no fastembed import.
The one real-model search lives behind the ``slow`` marker at the bottom.
"""

import sys
import types
from pathlib import Path

import numpy as np
import pytest

from orchestrator.memory.store import StateStore
from orchestrator.memory.vector import (
    FastEmbedder,
    VectorMemory,
    _from_blob,
    _to_blob,
    auto_memory,
    cosine_top_k,
    format_history_block,
    recall_context,
)


def test_blob_round_trip_preserves_floats() -> None:
    vec = [0.25, -1.5, 3.125, 0.0]
    blob = _to_blob(np.asarray(vec, dtype=np.float32))
    assert isinstance(blob, bytes)
    restored = _from_blob(blob)
    assert restored.dtype == np.float32
    assert restored.tolist() == pytest.approx(vec)


def test_cosine_ranks_identical_first() -> None:
    query = np.asarray([1.0, 0.0], dtype=np.float32)
    matrix = np.asarray(
        [[1.0, 0.0], [0.7071, 0.7071], [-1.0, 0.0]], dtype=np.float32
    )
    scores, indices = cosine_top_k(query, matrix, top_k=3)
    assert indices[0] == 0
    assert scores[0] == pytest.approx(1.0)
    assert list(indices) == [0, 1, 2]  # descending similarity
    assert scores[2] == pytest.approx(-1.0)


def test_cosine_top_k_limits_results() -> None:
    query = np.asarray([1.0, 0.0], dtype=np.float32)
    matrix = np.tile(np.asarray([1.0, 0.0], dtype=np.float32), (5, 1))
    scores, indices = cosine_top_k(query, matrix, top_k=2)
    assert len(scores) == len(indices) == 2


def test_cosine_top_k_handles_zero_norm_rows_without_nan() -> None:
    """Empty-text embeddings must score 0, never poison the ranking with NaN."""
    query = np.asarray([1.0, 1.0], dtype=np.float32)
    matrix = np.asarray([[0.0, 0.0], [1.0, 1.0]], dtype=np.float32)
    scores, indices = cosine_top_k(query, matrix, top_k=2)
    assert indices[0] == 1
    assert scores[1] == 0.0
    assert not any(np.isnan(scores))


def test_cosine_empty_matrix_returns_empty() -> None:
    query = np.asarray([1.0], dtype=np.float32)
    scores, indices = cosine_top_k(query, np.empty((0, 1), dtype=np.float32), top_k=5)
    assert scores.size == indices.size == 0


# -- VectorMemory with a fake embedder ----------------------------------------


class FakeEmbedder:
    """Deterministic vector lookup; unknown texts embed to the zero vector."""

    def __init__(self, vectors: dict[str, list[float]], dim: int = 2) -> None:
        self.vectors = vectors
        self.dim = dim
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[np.ndarray]:
        self.calls.append(list(texts))
        return [
            np.asarray(self.vectors.get(t, [0.0] * self.dim), dtype=np.float32)
            for t in texts
        ]


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    s = StateStore(tmp_path / "data" / "orchestrator.db")
    s.init_schema()
    yield s
    s.close()


def make_memory(store: StateStore, vectors: dict[str, list[float]]) -> VectorMemory:
    return VectorMemory(store, embedder=FakeEmbedder(vectors))


def test_upsert_then_search_finds_exact_entry(store: StateStore) -> None:
    memory = make_memory(store, {"conflict decision": [1.0, 0.0]})
    memory.upsert("conflict decision", source="knowledge", ref="repo_map.md", title="run 1")

    hits = memory.search("conflict decision", top_k=3)

    assert len(hits) == 1
    assert hits[0]["score"] == pytest.approx(1.0)
    assert hits[0]["source"] == "knowledge"
    assert hits[0]["ref"] == "repo_map.md"
    assert hits[0]["title"] == "run 1"
    assert hits[0]["body"] == "conflict decision"


def test_search_ranks_most_similar_first(store: StateStore) -> None:
    memory = make_memory(
        store,
        {
            "conflict entry": [1.0, 0.0],
            "database entry": [0.0, 1.0],
            "unrelated": [-1.0, 0.0],
        },
    )
    for ref, text in [("a", "conflict entry"), ("b", "database entry"), ("c", "unrelated")]:
        memory.upsert(text, source="report", ref=ref)

    hits = memory.search("conflict entry", top_k=2)

    assert [h["ref"] for h in hits] == ["a", "b"]
    assert hits[0]["score"] >= hits[1]["score"]


def test_search_respects_top_k_and_empty_store(store: StateStore) -> None:
    memory = make_memory(store, {})
    assert memory.search("anything", top_k=5) == []
    for n in range(4):
        memory.upsert(f"t{n}", source="report", ref=f"r{n}")
    vectors = {f"t{n}": [float(n), 1.0] for n in range(4)}
    memory.embedder = FakeEmbedder(vectors)
    hits = memory.search("t3", top_k=2)
    assert len(hits) == 2


def test_search_skips_entries_of_other_dimensions(store: StateStore) -> None:
    """Mixed-dim histories must never reach the ranking math (dim filter)."""
    memory = make_memory(store, {"two dim": [1.0, 0.0]})
    memory.upsert("two dim", source="knowledge", ref="a")
    memory.store.upsert_vector_entry(
        source="knowledge", ref="b", title="", body="three dim",
        dim=3, embedding=_to_blob(np.ones(3, dtype=np.float32)),
    )

    hits = memory.search("two dim", top_k=5)

    assert [h["ref"] for h in hits] == ["a"]


def test_upsert_same_ref_replaces_not_duplicates(store: StateStore) -> None:
    memory = make_memory(store, {"text one": [1.0, 0.0]})
    memory.upsert("text one", source="report", ref="w-1")
    memory.upsert("text one", source="report", ref="w-1")
    assert len(memory.search("text one", top_k=10)) == 1


def test_fastembedder_loads_model_lazily_and_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ONNX model is heavy: construction must stay free and the model
    must load exactly once, on the first embed() call."""
    created: list[str] = []

    class StubModel:
        def __init__(self, model_name: str) -> None:
            created.append(model_name)

        def embed(self, texts: list[str]) -> list[np.ndarray]:
            return [np.full(3, 0.5, dtype=np.float32) for _ in texts]

    monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=StubModel))

    embedder = FastEmbedder()
    assert created == [], "construction must not touch fastembed"

    first = embedder.embed(["a"])
    second = embedder.embed(["b"])

    assert created == [FastEmbedder.DEFAULT_MODEL_NAME]
    assert len(first) == len(second) == 1


# -- retrieval seam helpers ----------------------------------------------------


def test_format_history_block_renders_titles_scores_snippets() -> None:
    block = format_history_block(
        [
            {"title": "reconciliation policy", "ref": "d.md", "score": 0.61,
             "body": "Lead dispatches one\nreconciliation worker per conflict."},
            {"title": "", "ref": "w-9", "score": 0.10, "body": "short"},
        ]
    )
    lines = block.splitlines()
    assert lines[0].startswith("Relevant history")
    assert "reconciliation policy (score 0.61)" in lines[1]
    assert "reconciliation worker per conflict" in lines[1]
    assert "w-9 (score 0.10): short" in lines[2]


def test_format_history_block_truncates_long_bodies() -> None:
    block = format_history_block(
        [{"title": "t", "ref": "r", "score": 0.5, "body": "x" * 500}]
    )
    snippet_line = block.splitlines()[1]
    assert len(snippet_line) < 300
    assert snippet_line.endswith("...")


def test_format_history_block_empty_hits_is_empty_string() -> None:
    assert format_history_block([]) == ""


def test_recall_context_none_memory_or_empty_store_is_empty(store: StateStore) -> None:
    assert recall_context(None, "any goal") == ""
    memory = make_memory(store, {})
    assert recall_context(memory, "any goal") == "", (
        "empty vector store must short-circuit before any model load"
    )


def test_recall_context_formats_seeded_history(store: StateStore) -> None:
    memory = make_memory(store, {"decision text": [1.0, 0.0]})
    memory.upsert("decision text", source="knowledge", ref="d.md", title="the decision")

    block = recall_context(memory, "decision text", top_k=2)

    assert block.startswith("Relevant history")
    assert "the decision" in block


def test_recall_context_survives_embedder_failure(store: StateStore) -> None:
    """Tier-3 is strictly additive — a broken embedder must not break planning."""

    class ExplodingEmbedder:
        def embed(self, texts):
            raise RuntimeError("model download failed")

        # store attribute accessed before embed; reuse the real one

    memory = make_memory(store, {"decision text": [1.0, 0.0]})
    memory.upsert("decision text", source="knowledge", ref="d.md", title="t")
    memory.embedder = ExplodingEmbedder()

    assert recall_context(memory, "decision text") == ""


def test_auto_memory_disabled_by_kill_switch(store: StateStore, monkeypatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_SEMANTIC", "0")
    assert auto_memory(store) is None


def test_auto_memory_enabled_when_fastembed_importable(store: StateStore, monkeypatch) -> None:
    monkeypatch.delenv("ORCHESTRATOR_SEMANTIC", raising=False)
    memory = auto_memory(store)
    assert isinstance(memory, VectorMemory)


def test_auto_memory_silently_off_without_fastembed(store: StateStore, monkeypatch) -> None:
    monkeypatch.delenv("ORCHESTRATOR_SEMANTIC", raising=False)
    monkeypatch.setitem(sys.modules, "fastembed", None)  # import → ImportError
    assert auto_memory(store) is None


@pytest.mark.slow
def test_real_model_semantic_recall(tmp_path: Path) -> None:
    """Integration: the actual MiniLM model ranks a paraphrased natural-language
    query against seeded history. Downloads ~90MB ONNX on first use."""
    pytest.importorskip("fastembed")
    store = StateStore(tmp_path / "data" / "orchestrator.db")
    store.init_schema()
    try:
        memory = VectorMemory(store)  # real FastEmbedder
        memory.upsert(
            "Merge conflicts between managers are resolved by the Domain Lead,"
            " which dispatches one reconciliation worker per conflict (capped once).",
            source="knowledge", ref="decisions.md#reconciliation", title="reconciliation policy",
        )
        memory.upsert(
            "The orchestrator database runs in WAL mode with busy_timeout retries.",
            source="knowledge", ref="decisions.md#sqlite", title="sqlite policy",
        )
        hits = memory.search("how do we resolve merge conflicts?", top_k=2)
        assert hits[0]["title"] == "reconciliation policy"
    finally:
        store.close()
