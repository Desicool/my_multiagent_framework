"""Inbox data structures and constants.

Defines:
    - ``Message`` — the dataclass for inter-agent messages.
    - ``INBOX_CAP`` — hard cap on per-agent inbox queue size.

These are extracted from ``beidou/primitives/core.py`` to keep the engine
layer self-contained and SDK-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass

# Hard cap on per-agent inbox queue size (docs/limits.md #3).
INBOX_CAP = 1000


@dataclass
class Message:
    from_id: str        # sender agent_id, or "beidou" for system messages
    content: str
    ts: float           # unix ts
    message_id: str
    kind: str = "user"  # "user" | "terminate"


__all__ = ["INBOX_CAP", "Message"]
