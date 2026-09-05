"""Scaffold smoke test: every stub module imports cleanly.

Per-feature test files mirroring the source structure (e.g.
orchestrator/memory/store.py → tests/memory/test_store.py) are added as each
phase's feature is implemented, per AGENTS.md.
"""

import importlib

import pytest

MODULES = [
    "orchestrator",
    "orchestrator.contracts",
    "orchestrator.main",
    "orchestrator.logging_setup",
    "orchestrator.graph.architect",
    "orchestrator.graph.domain_lead",
    "orchestrator.graph.manager",
    "orchestrator.graph.worker",
    "orchestrator.graph.build_graph",
    "orchestrator.execution.runner",
    "orchestrator.execution.cli_runner",
    "orchestrator.execution.worktree_manager",
    "orchestrator.execution.tests_runner",
    "orchestrator.eval.source",
    "orchestrator.eval.replay",
    "orchestrator.eval.compare",
    "orchestrator.cli_eval",
    "orchestrator.memory.store",
    "orchestrator.memory.vector",
    "orchestrator.memory.knowledge_docs",
    "orchestrator.governance.budget",
    "orchestrator.governance.retry_policy",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports(module_name: str) -> None:
    importlib.import_module(module_name)
