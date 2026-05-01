"""Agent lifecycle data structures and constants.

Defines:
    - ``AgentRecord`` — per-agent runtime state dataclass.
    - ``CONTRACT_STRIKES`` — max consecutive contract violations before escalation.
    - ``CRASH_STRIKES`` — max consecutive crashes before escalation.

Extracted from ``beidou/orchestrator.py`` and ``beidou/primitives/core.py``
to keep the engine layer self-contained and SDK-agnostic.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

# Strike limits.
# CONTRACT_STRIKES is in docs/limits.md #5.
# CRASH_STRIKES is in docs/agent-runtime.md §5.1.
CONTRACT_STRIKES = 3
CRASH_STRIKES = 3


@dataclass
class AgentRecord:
    agent_id: str
    task_id: str
    team_id: str | None                # team this agent is a MEMBER of; None for teamless root
    role: str
    skill_name: str
    model: str | None
    inbox: asyncio.Queue
    # Field name is ``create_team_lock`` for backward-compat with test fixtures;
    # access via the ``spawn_lock`` property (limits.md #5 rename).
    create_team_lock: asyncio.Lock
    name: str = ""                    # human-readable display name, e.g. "frontend-engineer-a3b2"
    last_status: str = "working"
    last_status_detail: str = ""
    contract_strikes: int = 0
    run_task: asyncio.Task | None = None
    terminate_consumed: bool = False
    terminate_grace_deadline: float | None = None
    total_tokens: int = 0
    # Completion-review state (Phase 2 foundation — bd issue 8z3).
    completion_pending: bool = False
    completion_pending_ts: float | None = None
    last_progress_ts: float = field(default_factory=time.time)
    last_drain_ts: float | None = None
    review_ping_count: int = 0
    # Watchdog fields (bd issue qj2).
    inflight_tools: int = 0
    idle_nudge_count: int = 0
    # Plan-task association. Set when this agent was spawned via spawn_for_task;
    # stays None for the root agent and for legacy create_team-spawned members.
    plan_task_id: str | None = None
    plan_id: str | None = None
    # Crash recovery (agent-runtime.md §5.1).
    crash_strikes: int = 0
    last_session_id: str | None = None
    last_crash_stderr: str = ""

    @property
    def spawn_lock(self) -> asyncio.Lock:
        """Alias for create_team_lock (limits.md #5 rename; field kept for test compat)."""
        return self.create_team_lock


__all__ = ["AgentRecord", "CONTRACT_STRIKES", "CRASH_STRIKES"]
