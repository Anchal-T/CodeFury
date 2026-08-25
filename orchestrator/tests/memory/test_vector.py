"""Tests for Tier-3 vector math (orchestrator.memory.vector).

Hermetic: fake embeddings only — no model download, no fastembed import.
"""

import numpy as np
import pytest

from orchestrator.memory.vector import _from_blob, _to_blob, cosine_top_k


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
    matrix = np.eye(5, dtype=np.float32)  # every row identical to the query
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
