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
Run ocr(open code review) tool to review the codebase, the cli tool is installed and configured on the system, do a review before commiting to the repository.

## Discuss Before Diverging

- **Hard circuit breaker on reasoning and scouting.** When a request leaves 2 or more plausible product, scope, or architectural interpretations:
  1. **Stop simulating branches.** State each plausible fork and its core trade-off in one sentence. Do NOT mentally design implementation details, edge cases, or speculative refactors for unchosen paths.
  2. **Cap pre-alignment scouting to one pass.** Run at most 3 direct file reads or 1 parallel read-only subagent batch (research / Explore) to verify codebase facts before asking. (Full parallel scouting for unambiguous tasks remains unrestricted.)
  3. **Zero unrequested scope.** Never invent hypothetical future requirements or bundle "while we're here" cleanups.
- **Single-turn check-in** (overrides any multi-turn brainstorming / no-menu rule): unless the user explicitly asks for open brainstorming or an interactive design interview (e.g. /grill-me), share known codebase facts in 2–3 sentences and resolve ambiguity in one check-in, merging any model or execution-mode recommendation into it:
  - **Finite choices** (approaches, scope boundary, execution mode): call the structured question tool (ask_question / AskUserQuestion / AskQuestion) with 2–4 concrete options, recommended first.
  - **Open intent** ("what goal do you want?"): ask 1 concise plain-text question without steering options.
