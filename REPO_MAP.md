# REPO_MAP

One line per file. Updated in the same commit as any file add/remove/rename (AGENTS.md).

- `.gitignore` — root ignore rules (keeps the local-only build plan untracked).
- `AGENTS.md` — coding conventions for agents working in this repo.
- `README.md` — git branching & commit guide (main/dev/feature workflow).
- `REPO_MAP.md` — this file: one line per file in the repo.
- `orchestrator-build-plan.md` — architecture & phased build plan (untracked, local-only).
- `orchestrator/.env.example` — template for secrets (Z_AI_API_KEY).
- `orchestrator/.gitignore` — ignores Python caches, .venv, .env, and ephemeral workspaces/data/logs.
- `orchestrator/config.yaml` — budgets, concurrency caps, model effort mapping, paths.
- `orchestrator/conftest.py` — puts the orchestrator project dir on sys.path for pytest.
- `orchestrator/requirements.txt` — dependencies (langgraph, pydantic, pyyaml, psutil, click, python-dotenv).
- `orchestrator/orchestrator/__init__.py` — package marker + version.
- `orchestrator/orchestrator/main.py` — CLI entrypoint (start/status/approve/logs).
- `orchestrator/orchestrator/contracts.py` — Task/Report pydantic models, the only objects crossing levels.
- `orchestrator/orchestrator/logging_setup.py` — JSONL structured logging setup.
- `orchestrator/orchestrator/graph/__init__.py` — package marker for graph nodes.
- `orchestrator/orchestrator/graph/architect.py` — Level 3 node: epic ownership, human interaction.
- `orchestrator/orchestrator/graph/domain_lead.py` — Level 2 node: per-domain coordination, conflict resolution.
- `orchestrator/orchestrator/graph/manager.py` — Level 1 node: worker fan-out, merge gating, retries.
- `orchestrator/orchestrator/graph/worker.py` — Level 0 node: one ZCode subprocess per task.
- `orchestrator/orchestrator/graph/build_graph.py` — wires all levels into one LangGraph StateGraph.
- `orchestrator/orchestrator/execution/__init__.py` — package marker for execution layer.
- `orchestrator/orchestrator/execution/zcode_runner.py` — cross-platform ZCode CLI subprocess wrapper.
- `orchestrator/orchestrator/execution/worktree_manager.py` — git worktree add/merge/discard/cleanup.
- `orchestrator/orchestrator/memory/__init__.py` — package marker for memory tier.
- `orchestrator/orchestrator/memory/store.py` — SQLite (WAL) state store + LangGraph SqliteSaver.
- `orchestrator/orchestrator/memory/vector.py` — optional Tier-3 fastembed+numpy semantic search.
- `orchestrator/orchestrator/memory/knowledge_docs.py` — append-only markdown knowledge files (Tier 1).
- `orchestrator/orchestrator/governance/__init__.py` — package marker for governance.
- `orchestrator/orchestrator/governance/budget.py` — per-level token budget tracking and hard caps.
- `orchestrator/orchestrator/governance/retry_policy.py` — retry/escalation caps.
- `orchestrator/domains/PROJECT_STATE.md` — epic-level project state (Architect-maintained).
- `orchestrator/domains/backend/repo_map.md` — backend domain knowledge (Domain Lead-maintained).
- `orchestrator/domains/infra/repo_map.md` — infra domain knowledge (Domain Lead-maintained).
- `orchestrator/tests/test_scaffold.py` — smoke test: all stub modules import.
