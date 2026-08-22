# AGENTS.md

- Follow OOP principles: encapsulation, single responsibility, clear class boundaries — no god objects.
- Keep components loosely coupled: depend on interfaces/contracts, not concrete implementations across modules.
- Maintain `REPO_MAP.md` at the root: one line per file, stating what it does. Update it in the same commit as any file add/remove/rename.
- Keep files small and modular: split a file if it exceeds ~200-300 lines or handles more than one responsibility.
- After completing any feature, write unit tests in `tests/` mirroring the source folder structure (e.g. `orchestrator/memory/store.py` → `tests/memory/test_store.py`).
- Run tests before marking a task done: `pytest -q`. Do not report a feature complete if tests fail.
- Use `pathlib.Path` and list-form `subprocess` calls only — code must run on both Linux and Windows.
- No new heavy dependencies (torch, docker, redis) without explicit approval — this project must stay lightweight and local-only.
- Commit messages: `<scope>: <what changed>`, one logical change per commit.
- When blocked or uncertain about architecture, stop and report — don't silently improvise a workaround.
