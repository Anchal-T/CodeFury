# CodeFury Orchestrator

A local-only, multi-agent coding orchestrator: a hierarchy of roles (Worker, Manager, Domain Lead, Architect) decomposes software work into tasks, runs coding agents in isolated git worktrees, reviews their reports, and merges or escalates the results.

## Language

**Task**:
A unit of work at any hierarchy level, carrying goal, deliverable, status and domain. The only object that crosses level seams.
_Avoid_: job, item, ticket

**Report**:
The outcome record a finished Task produces: test results, diff reference, token usage, blockers. Exactly one latest Report per dispatch round.
_Avoid_: result, output, log

**Worker**:
Level-0 role. Runs one Task in an isolated git worktree and produces a Report.
_Avoid_: agent, subprocess (that's the mechanism, not the role)

**Manager**:
Level-1 role. Decomposes its Task into Worker Tasks, reviews their Reports, merges passing branches, retries failures within caps.
_Avoid_: supervisor, coordinator

**Domain Lead**:
Level-2 role. Coordinates Managers of one domain and resolves cross-module merge conflicts through capped reconciliation dispatches.
_Avoid_: lead, domain manager

**Architect**:
Level-3 role. Plans an epic's Domain Leads behind a human approval gate and finalizes the epic when all leads resolve.

**Epic**:
A top-level Task owned by the Architect, decomposed into Domain Lead Tasks that start in pending_approval.

**Domain**:
A named slice of the repo (e.g. backend, infra) that a Domain Lead owns; knowledge for it accumulates in the domain's repo_map.md.

**Reconciliation**:
A single retry-capped dispatch that resolves one Manager's cross-module merge conflict by integrating the conflicting branches.
_Avoid_: re-run, fix-up task

**Blocker**:
A structured reason string attached to a Report explaining why it did not pass; conflict blockers follow the conflict-blocker convention.

**Approval gate**:
The human checkpoint where a pending_approval Task is flipped to approved before work may begin on it.

**Integration gate**:
The single post-merge test run against the assembled repo root, catching individually-passing branches that combine into a broken tree.

**Retry cap**:
The hard maximum number of attempts a Task may receive before it fails permanently; enforced in code, not just config.

**Checkpoint** (Phase 5):
A persisted LangGraph state snapshot for one lead thread in the same SQLite file (checkpoints/writes tables via the async checkpointer), letting the graph continue work across processes.
_Avoid_: savepoint, snapshot

**Thread id** (Phase 5):
The stable LangGraph configurable `lead:<task id>` that binds one physical lead to one resumable thread; the same id in every process is what makes killed-session resume deterministic.

**Resume** (Phase 5):
Re-invoking a lead graph with the same thread id and input None so LangGraph continues from the last checkpoint instead of restarting; the store's status is reconciled from the returned outcome.

**Tier-1 memory / knowledge context** (Phase 5):
The cheapest memory tier — git-tracked markdown sections (repo_map.md per domain, PROJECT_STATE.md) read via KnowledgeDocs and prepended to worker prompts and planning seams. The future LLM seam for decomposers.
