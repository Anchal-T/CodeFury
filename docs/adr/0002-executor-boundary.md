# 0002. Executor boundary

**Status** — Proposed, pending owner review. [ANC-5](https://linear.app/anchal-t/issue/ANC-5/design-the-executor-contract-and-audit-local-execution-seams)

## Context

The Worker currently prepares the prompt, creates a Git worktree, runs the agent and tests, commits, and saves a `Report`. The worktree starts at the current `HEAD`. Creating another worktree for the same task clears the previous worktree and branch, which can destroy work that is running or waiting for review. We want to change where a Worker runs while keeping the existing task hierarchy and review behavior.

## Decision

The Worker sends one attempt to an Executor and waits for its result. In ANC-6, a local adapter will reuse the existing Git, runner, and test code. The Executor owns checkout, agent execution, Worker tests, and the attempt commit. The Worker owns prompts, the budget, token accounting, logs, memory, and `Task` and `Report` persistence. The Manager owns review, retry, and merge. The Domain Lead still owns reconciliation and its merge. Post-merge integration tests stay with the graph.

A blocking `execute(request)` would offer the simplest call for the local adapter. The proposed handle lets callers check queued work and cancel it before it starts, and gives a repeated submission a stable run ID. The first Worker caller still waits for each result using its existing concurrency limit.

## Proposed contract

This sketch describes the interface for [ANC-6](https://linear.app/anchal-t/issue/ANC-6/extract-localexecutor-and-preserve-existing-graph-behavior). The transport values contain IDs, strings, and typed results. Each adapter resolves `repo_id` from its own repository configuration and receives credentials from its runtime.

```python
from dataclasses import dataclass
from typing import Literal, Protocol

TerminalState = Literal["succeeded", "failed", "timed_out", "cancelled"]
ExecutionState = Literal["queued", "running"] | TerminalState


@dataclass(frozen=True)
class ResourceRequest:
    cpu_millicores: int | None = None
    memory_bytes: int | None = None


@dataclass(frozen=True)
class ExecutionRequest:
    repo_id: str
    task_id: str
    attempt: int
    base_sha: str
    prompt: str
    test_command: tuple[str, ...]
    worker_timeout_s: float
    test_timeout_s: float
    commit_message: str
    resources: ResourceRequest | None = None


@dataclass(frozen=True)
class ExecutionHandle:
    execution_id: str


@dataclass(frozen=True)
class ExecutionResult:
    execution_id: str
    state: TerminalState
    worker_exit_code: int | None
    test_exit_code: int | None
    worker_timed_out: bool
    test_timed_out: bool
    worker_duration_s: float | None
    tests_passed: bool
    blocked_marker: bool
    stdout: str
    stderr: str
    test_output: str
    diff_ref: str | None
    commit_sha: str | None
    error: str | None = None


class Executor(Protocol):
    def submit(self, request: ExecutionRequest) -> ExecutionHandle: ...
    def status(self, handle: ExecutionHandle) -> ExecutionState: ...
    def wait(self, handle: ExecutionHandle) -> ExecutionResult: ...
    def cancel(self, handle: ExecutionHandle) -> bool: ...
```

- `submit` validates a known repository ID, a nonempty task ID and test argument list, a positive attempt number, finite positive timeouts, and a full Git commit ID. Resource hint values must be positive when supplied. Resource enforcement belongs to a later milestone.
- `status` reports the current state. `wait` blocks until a terminal result is available and returns the same result on repeated calls. `status`, `wait`, and `cancel` raise a lookup error for an unknown ID.
- The initial state is `queued`. The Executor atomically claims `queued → running` before checkout. A running attempt finishes as `succeeded`, `failed`, or `timed_out`. A queued attempt can move directly to `cancelled`. The result stays fixed after any terminal transition.
- `cancel` returns `True` when it changes `queued` to `cancelled` and prevents launch. It returns `False` once the attempt is running or terminal. The existing runner can terminate its subprocess tree on timeout. The first local adapter supports queued cancellation only.
- A step that never returns an exit code uses `None`. When the local adapter hands off a branch, `diff_ref` names it and `commit_sha` is its tip. The tip equals `base_sha` if the attempt made no commits. A failure before handoff can leave both fields unset.

## Proposed flow and retries

1. The graph supplies a one-based attempt number. The standalone `run` command starts at attempt 1. The Worker claims the attempt once in-process. A repeated Worker call joins that attempt or reuses its completed report. The first call saves `in_progress`, checks budget, and records the existing budget blocker if exhausted. A denied attempt submits no work.
2. The Worker builds the prompt with the existing knowledge context and captures the integration `HEAD` once per attempt. It retains the original request for duplicate calls so that a moving `HEAD` or changing context cannot alter a previously submitted request.
3. Under one process-local lock, the local adapter checks the request key `(repo_id, task_id, attempt)` and reserves the actual branch and worktree names. An identical request returns the original handle. The same key with different inputs is rejected and the original run is preserved. A branch or worktree collision rejects a new attempt before checkout and surfaces as a blocker. Resolve repository aliases to the same Git repository and account for task-ID sanitization and case-insensitive paths. Release the lock before blocking or running subprocesses.
4. The adapter verifies the pinned SHA resolves to a commit before any cleanup. It starts the worktree with `git worktree add -b <branch> <path> <base_sha>`. An older commit is usable while it exists locally. A missing commit fails the attempt before changing a branch or worktree. Preserve `orchestrator/worker-<sanitized-task-id>` because the existing merge paths look up that branch from the task ID.
5. After the agent returns a result, run the tests and attempt to commit, preserving the current order for nonzero exits and reported timeouts. Record whether `BLOCKED.md` exists. On a normal return, either timeout takes precedence over other failures. A nonzero agent exit, a test failure, or `BLOCKED.md` produces `failed`. A checkout, runner, or commit exception produces `failed` with an execution error. A successful agent and test run with no blocker produces `succeeded`, including a no-op commit. Preserve the existing test policy that exits 0 and 5 pass.
6. The Worker maps the result to the existing `Report`, accounts for tokens once from the current `TOKENS_USED` output, and moves the `Task` to `review` on success or `failed` otherwise. Preserve the existing timeout, test, exit-code, and blocked-file messages. Map cancellation to a failed report with a cancellation blocker and no worker token usage. Both `wait` and `status` are free of budget and persistence side effects. The Worker guards finalization by execution ID so repeated calls cannot charge twice, save another report, or replace a newer attempt's report.
7. Execution finishing leaves the branch reserved through review. The local merge path checks the recorded branch tip before merging. The Manager marks a merged Task `done`, returns an eligible failure to `pending` for retry, and treats budget blockers as terminal. An injected callback signals review resolution after the Manager or Domain Lead decides to merge, reject, or retry. For a merge, signal after it completes. The local registry uses the repository ID, task ID, and attempt from the review to release the reservation. It then permits a retry of the same task and rejects distinct task IDs that map to a retained branch or workspace. The standalone CLI signals resolution after saving the report and leaves the branch available for inspection. Existing branches or worktrees whose ownership the process cannot prove require explicit resolution before reuse.

The registry and execution IDs last for the lifetime of the process. The graph retains its existing checkpoint and retry behavior. A restart loses the registry, so an unknown leftover workspace is surfaced as a blocker. Distributed scheduling, durable exactly-once execution, Kubernetes, new infrastructure, and remote Git publishing are outside this decision.

## Call sites audited on `dev`

The audit uses commit `9eaa42eb2ffd67032dc01d2f13de65d834d5850c`. The links use its file layout.

| Call site | What ANC-6 preserves |
| --- | --- |
| [`main.py`](../../orchestrator/orchestrator/main.py#L92) | `run` calls `run_worker_task` directly. `manage` supplies the runner and worktrees to `build_graph`. `lead` supplies them to `build_lead_graph`. Construct the adapter at these entry points. |
| [`cli_epic.py`](../../orchestrator/orchestrator/cli_epic.py#L59) and [`graph/architect.py`](../../orchestrator/orchestrator/graph/architect.py#L136) | Epic `start` and approved resume construct a lead graph through a factory. Share the same local execution registry across its leads. Preserve the approval and checkpoint flow. |
| [`graph/build_graph.py`](../../orchestrator/orchestrator/graph/build_graph.py#L57) and [`graph/lead_graph.py`](../../orchestrator/orchestrator/graph/lead_graph.py#L84) | Pass the adapter through the regular Worker and reconciliation Worker paths. Forward the existing attempt number and hold the shared semaphore through the blocking wait in `asyncio.to_thread`. |
| [`graph/worker.py`](../../orchestrator/orchestrator/graph/worker.py#L105) and [`execution/zcode_runner.py`](../../orchestrator/orchestrator/execution/zcode_runner.py#L65) | Move checkout, runner invocation, Worker tests, and commit out of `_run_pipeline`. Preserve the timeout behavior and report generation. `run_tests` currently records pass and timeout flags. Capture the test exit code when wiring the result in ANC-6. |
| [`execution/worktree_manager.py`](../../orchestrator/orchestrator/execution/worktree_manager.py#L91) | Accept the pinned commit explicitly. Guard branch and worktree reuse before the existing cleanup path. Keep the current branch naming convention. |
| [`graph/manager.py`](../../orchestrator/orchestrator/graph/manager.py#L39) and [`graph/domain_lead.py`](../../orchestrator/orchestrator/graph/domain_lead.py#L50) | Keep task-ID merges, the existing retry cap, and conflict-only reconciliation. Reconciliation uses its own task ID and goes through the same Executor. |
| [`graph/outcome_recorder.py`](../../orchestrator/orchestrator/graph/outcome_recorder.py#L55) and [`graph/lead_graph.py`](../../orchestrator/orchestrator/graph/lead_graph.py#L259) | These paths also call `run_tests` for integration checks. Keep their tests rooted at the integration checkout and preserve their pass and timeout semantics. |

## Baseline

Recorded on 2026-11-28 from a clean checkout of the audited `dev` commit. Tests ran on Linux with CPython 3.12.13, pytest 9.2.0, LangGraph 1.2.24, and `langgraph-checkpoint-sqlite` 3.1.1 in a temporary environment installed from `orchestrator/requirements.txt`.

| Command from `orchestrator/` | Result |
| --- | --- |
| `pytest -q` | 284 passed, 1 deselected. The default configuration excludes the slow embedding-model test. |
| `pytest -q tests/execution tests/graph/test_worker.py` | 45 passed. |

## Acceptance checks for ANC-7

Use temporary Git repositories and the existing fake worker. Exercise the public methods of the contract and keep the existing graph tests.

| Case | Expected behavior |
| --- | --- |
| Success and no-op | The Report goes to review, the branch and tip are available, and a no-op keeps the base as the tip. |
| Agent or tests fail or time out | A blocker and the available outputs survive in the report. A reported timeout gives `timed_out`. Exit 5 keeps its existing pass behavior. |
| `BLOCKED.md` or an execution exception | The Report records the blocker or error and the Task is failed. |
| Same submission, changed inputs, and repeated wait | Identical requests share one handle and worktree. Changed inputs under the same key are rejected. Repeated waiting preserves one result, one budget debit, and one report. |
| Overlapping attempts and sanitized-name collisions | A queued, running, or review-pending attempt owns its branch. A new attempt can reuse it only after review resolves and the Manager authorizes a retry. An unowned leftover is rejected. |
| `HEAD` moves or the requested commit is missing | The worktree starts at the exact requested commit. A missing commit leaves existing branches and worktrees intact. |
| Cancellation | A queued cancellation prevents launch. A running cancellation returns `False`. A race produces one terminal result. |
| Budget, concurrency, and graph wiring | Budget exhaustion makes zero submissions. The shared cap holds for ordinary and reconciliation Workers. `run`, `manage`, `lead`, and epic resume keep their current outcomes and integration checks. |

Review this design with the owner before starting ANC-6. Use the contract and the baseline above in ANC-7.
