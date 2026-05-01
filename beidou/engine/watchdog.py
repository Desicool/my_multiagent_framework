"""Watchdog tunables extracted from ``beidou/orchestrator.py``.

These are implementation constants, NOT in docs/limits.md. They define the
cadence and thresholds for the three-pass watchdog (review escalation, liveness
nudge, terminate-grace backstop).
"""

from __future__ import annotations

WATCHDOG_INTERVAL_S = 30.0
REVIEW_PING_INTERVAL_S = 60.0
IDLE_NUDGE_S = 120.0
MAX_PINGS_BEFORE_ESCALATION = 3
TERMINATE_GRACE_S = 60.0


__all__ = [
    "IDLE_NUDGE_S",
    "MAX_PINGS_BEFORE_ESCALATION",
    "REVIEW_PING_INTERVAL_S",
    "TERMINATE_GRACE_S",
    "WATCHDOG_INTERVAL_S",
]
