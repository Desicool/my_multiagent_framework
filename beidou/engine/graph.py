"""Team graph data structures and invariant helpers.

Defines:
    - ``USER_SENTINEL`` — sentinel leader-id for the root agent.
    - ``TeamRecord`` — dataclass for tracking a team in the graph.
    - ``_slug_role`` — name-generation helper for agent display names.

These are extracted from ``beidou/orchestrator.py`` to keep the engine layer
self-contained and SDK-agnostic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# Sentinel leader-id for the root agent.  The root agent has no team and no
# leader; USER_SENTINEL is stored as ``leader_id`` in any context object that
# needs a "who leads the root?" answer (e.g. spawn context template_vars).
USER_SENTINEL = "__user__"


@dataclass
class TeamRecord:
    team_id: str
    name: str
    leader_id: str                    # MUST equal caller_id of create_team / spawn_for_task
    depth: int                        # 0 for root
    member_ids: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    parent_team_id: str | None = None
    # ``task`` is dead metadata — only written by legacy create_team callers,
    # never read by the orchestrator. Kept as optional with a default so
    # existing test fixtures still pass; will be removed in a follow-up commit.
    task: str = ""


def _slug_role(role: str) -> str:
    """Convert a role string into a clean slug component.

    Steps: lowercase -> replace runs of [^a-z0-9] with '-' -> strip
    leading/trailing '-' -> collapse repeated '-' -> truncate to 24 chars ->
    fall back to 'agent' if the result is empty.
    """
    s = role.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    s = re.sub(r"-{2,}", "-", s)
    s = s[:24]
    return s or "agent"


__all__ = ["USER_SENTINEL", "TeamRecord", "_slug_role"]
