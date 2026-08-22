"""Level 1 — Manager node: fans out to workers via LangGraph Send, gates merges (plan §2)."""


def manager_node(state: dict) -> dict:
    """Fan out worker Tasks (Send), review diffs, merge or retry within caps."""
    raise NotImplementedError("Phase 2")
