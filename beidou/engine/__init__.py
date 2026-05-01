"""Beidou engine layer — SDK-agnostic orchestration logic.

This package is the inner core: data structures, team graph, inbox routing,
agent lifecycle, watchdog, and the internal event dispatcher. It MUST NOT
import from ``claude_agent_sdk`` or from ``beidou.agent``.

Modules:
    config:       EngineConfig and AgentConfig (the engine/agent data boundary)
    graph:        TeamRecord, team graph invariants, depth/fan-out caps
    inbox:        Message dataclass, inbox constants
    lifecycle:    AgentRecord dataclass, contract/crash limits
    watchdog:     Watchdog tunables
    dispatcher:   Internal event bus for eval handler subscriptions
"""

from beidou.engine.config import AgentConfig, EngineConfig
from beidou.engine.dispatcher import Dispatcher
from beidou.engine.graph import USER_SENTINEL, TeamRecord, _slug_role
from beidou.engine.inbox import INBOX_CAP, Message
from beidou.engine.lifecycle import CONTRACT_STRIKES, CRASH_STRIKES, AgentRecord
from beidou.engine.watchdog import (
    IDLE_NUDGE_S,
    MAX_PINGS_BEFORE_ESCALATION,
    REVIEW_PING_INTERVAL_S,
    TERMINATE_GRACE_S,
    WATCHDOG_INTERVAL_S,
)

__all__ = [
    "AgentConfig",
    "AgentRecord",
    "CONTRACT_STRIKES",
    "CRASH_STRIKES",
    "Dispatcher",
    "EngineConfig",
    "IDLE_NUDGE_S",
    "INBOX_CAP",
    "MAX_PINGS_BEFORE_ESCALATION",
    "Message",
    "REVIEW_PING_INTERVAL_S",
    "TeamRecord",
    "TERMINATE_GRACE_S",
    "USER_SENTINEL",
    "WATCHDOG_INTERVAL_S",
    "_slug_role",
]
