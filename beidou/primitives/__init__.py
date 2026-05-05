"""Beidou agent primitives (pure Python). See ``core`` for the implementation."""

from beidou.primitives.core import (
    CONTRACT_STRIKES,
    CRASH_STRIKES,
    INBOX_CAP,
    MAX_DEPTH,
    GatewayDeclined,
    Message,
    Orchestrator,
    Peer,
    PrimitiveError,
    ask_user,
    create_team,
    list_peers,
    report_status,
    send_message,
    terminate_child,
)
from beidou.primitives.mcp import build_mcp_server_for

__all__ = [
    "INBOX_CAP",
    "MAX_DEPTH",
    "CONTRACT_STRIKES",
    "CRASH_STRIKES",
    "PrimitiveError",
    "GatewayDeclined",
    "Message",
    "Peer",
    "Orchestrator",
    "send_message",
    "list_peers",
    "ask_user",
    "report_status",
    "create_team",
    "terminate_child",
    "build_mcp_server_for",
]
