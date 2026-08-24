# LevelGraph owns graph wiring; state schemas stay explicit

The level lifecycle (plan → dispatch → review → finalize) is duplicated across build_graph, lead_graph and architect. We extract a deep `LevelGraph` module that owns node wiring, routing, attempt counting and the lifecycle skeleton, with per-level behaviour injected as (decomposer, reviewer, finalizer/recorder). Reviewers return normalized `RoundDecision`/`DispatchSpec` values so routing stays inside LevelGraph.

Considered options: generating state TypedDicts per role was rejected — the LangGraph subgraph channel drop-list semantics (LeadState deliberately omitting subgraph channels so outputs are dropped/aggregated) are subtle invariants that must remain visible, reviewable code. State schemas stay hand-written in state.py and are passed to LevelGraph explicitly.

Consequences: adding a new automated level means writing a reviewer adapter + recorder config, not copying a graph skeleton. The Architect does not join the abstraction yet (status-driven, no dispatch); revisit if it gains automated rounds.
