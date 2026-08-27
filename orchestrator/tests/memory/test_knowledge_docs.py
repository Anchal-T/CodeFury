"""Knowledge docs tests (plan §3.5, roadmap Phase 3): missing-doc reads,
append-only timestamped sections, and history preservation."""

import re
from pathlib import Path

from orchestrator.memory.knowledge_docs import KnowledgeDocs

TIMESTAMP_RE = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"


def test_read_latest_missing_file_returns_empty_string(tmp_path: Path) -> None:
    docs = KnowledgeDocs()
    assert docs.read_latest(tmp_path / "domains" / "backend" / "repo_map.md") == ""


def test_append_section_creates_file_with_timestamped_section(tmp_path: Path) -> None:
    docs = KnowledgeDocs()
    target = tmp_path / "domains" / "backend" / "repo_map.md"

    docs.append_section(target, "round one", "merged module_a, module_b")

    assert target.is_file()
    text = target.read_text(encoding="utf-8")
    assert text.startswith("## round one — ")
    stamp = text.split("— ", 1)[1].split("\n", 1)[0]
    assert re.fullmatch(TIMESTAMP_RE, stamp), f"not UTC ISO timestamp: {stamp}"
    assert "\nmerged module_a, module_b\n" in text


def test_append_section_preserves_existing_history(tmp_path: Path) -> None:
    docs = KnowledgeDocs()
    target = tmp_path / "repo_map.md"
    target.write_text("# repo map\n", encoding="utf-8")

    docs.append_section(target, "first", "alpha")
    first_text = target.read_text(encoding="utf-8")
    docs.append_section(target, "second", "beta")

    text = target.read_text(encoding="utf-8")
    assert text.startswith(first_text), "append must never rewrite existing bytes"
    assert "# repo map\n" in text
    assert text.index("## first — ") < text.index("## second — ")
    assert "\nbeta\n" in text


def test_append_section_handles_file_without_trailing_newline(tmp_path: Path) -> None:
    docs = KnowledgeDocs()
    target = tmp_path / "repo_map.md"
    target.write_text("# no trailing newline", encoding="utf-8")

    docs.append_section(target, "after", "body")

    text = target.read_text(encoding="utf-8")
    assert text.startswith("# no trailing newline\n\n## after — ")


# -- Phase 7: section parsing for Tier-3 indexing ------------------------------


def test_parse_sections_splits_on_headings(tmp_path: Path) -> None:
    from orchestrator.memory.knowledge_docs import parse_sections

    doc = tmp_path / "repo_map.md"
    doc.write_text(
        "## lead run lead-1 — 2026-08-24T10:00:00Z\n\nbody one line\nbody two\n\n"
        "## lead run lead-2 — 2026-08-25T11:00:00Z\n\nsecond body\n",
        encoding="utf-8",
    )
    sections = parse_sections(doc.read_text(encoding="utf-8"))
    assert [(title, body) for title, body in sections] == [
        ("lead run lead-1 — 2026-08-24T10:00:00Z", "body one line\nbody two"),
        ("lead run lead-2 — 2026-08-25T11:00:00Z", "second body"),
    ]


def test_parse_sections_ignores_preamble_and_empty_docs(tmp_path: Path) -> None:
    from orchestrator.memory.knowledge_docs import parse_sections

    assert parse_sections("# just a title\nsome intro text\n") == []
    assert parse_sections("") == []
