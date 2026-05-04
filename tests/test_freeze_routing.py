"""Tests for FREEZE probe delivery synthesis gate (bd issue xylz).

The runtime gate in loop.py (detect_orphaned_freeze + ResultMessage branch)
catches v2 design-committee agents that wrote [FREEZE OK] / [FREEZE NACK] in
their assistant text but failed to deliver it via send_message.

Tests:
1. test_freeze_ok_synthesized_when_idle_report_status_on_v2_committee
2. test_freeze_ok_passthrough_when_send_message_already_called
3. test_freeze_nack_synthesized_with_reason_capture
4. test_no_synthesis_for_v1_or_non_committee_skills
"""
from __future__ import annotations

import pytest

from beidou.agent.loop import detect_orphaned_freeze
from beidou.agent.hooks import V2_DESIGN_COMMITTEE_SKILLS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

LEADER_ID = "orch_leader_001"


def _turn_tool_info_with_report_status() -> dict:
    """Turn where agent called report_status(state='idle') — wrong tool for FREEZE."""
    return {
        "tu_001": ("mcp__beidou__report_status", {"state": "idle"}),
    }


def _turn_tool_info_with_freeze_send_message(content: str = "[FREEZE OK]") -> dict:
    """Turn where agent correctly called send_message with FREEZE content to leader."""
    return {
        "tu_002": (
            "mcp__beidou__send_message",
            {"to": LEADER_ID, "content": content},
        ),
    }


def _turn_tool_info_with_send_message_to_wrong_target() -> dict:
    """Turn where send_message was called but to a peer, not the leader."""
    return {
        "tu_003": (
            "mcp__beidou__send_message",
            {"to": "some_peer_agent", "content": "[FREEZE OK]"},
        ),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_freeze_ok_synthesized_when_idle_report_status_on_v2_committee():
    """[FREEZE OK] in text + report_status(idle) but no send_message → synthesize."""
    assistant_text = (
        "I have reviewed requirements.md and it is stable.\n"
        "[FREEZE OK]\n"
        "No pending revisions."
    )
    turn_tool_info = _turn_tool_info_with_report_status()

    result = detect_orphaned_freeze(assistant_text, turn_tool_info, LEADER_ID)

    assert result is not None
    freeze_kind, freeze_content = result
    assert freeze_kind == "ok"
    assert "[FREEZE OK]" in freeze_content


def test_freeze_ok_passthrough_when_send_message_already_called():
    """[FREEZE OK] in text AND send_message already delivered it → no synthesis."""
    assistant_text = (
        "Requirements are stable. [FREEZE OK]\n"
    )
    turn_tool_info = _turn_tool_info_with_freeze_send_message("[FREEZE OK]")

    result = detect_orphaned_freeze(assistant_text, turn_tool_info, LEADER_ID)

    assert result is None


def test_freeze_nack_synthesized_with_reason_capture():
    """[FREEZE NACK] with reason captured in full content."""
    assistant_text = (
        "The precision metric is still under debate.\n"
        "[FREEZE NACK]: precision still under debate\n"
        "Will update once resolved."
    )
    turn_tool_info = _turn_tool_info_with_report_status()

    result = detect_orphaned_freeze(assistant_text, turn_tool_info, LEADER_ID)

    assert result is not None
    freeze_kind, freeze_content = result
    assert freeze_kind == "nack"
    assert "precision still under debate" in freeze_content


def test_no_synthesis_for_v1_or_non_committee_skills():
    """Synthesis gate only fires for V2 design-committee skill names.

    The detect_orphaned_freeze helper itself is skill-agnostic (the skill check
    lives in the loop integration site). This test verifies that the set
    V2_DESIGN_COMMITTEE_SKILLS does NOT include v1 skill names, confirming the
    integration guard would skip those agents.
    """
    v1_skill = "qa_engineer"
    non_committee_skill = "orchestrator_v2"

    assert v1_skill not in V2_DESIGN_COMMITTEE_SKILLS
    assert non_committee_skill not in V2_DESIGN_COMMITTEE_SKILLS

    # Even if the helper is called (it would not be in prod for these skills),
    # it returns the correct result based purely on text+tools — this is just
    # belt-and-suspenders validation that the helper itself is correct.
    assistant_text = "[FREEZE OK]\n"
    turn_tool_info = _turn_tool_info_with_report_status()
    result = detect_orphaned_freeze(assistant_text, turn_tool_info, LEADER_ID)
    # Helper alone would find the orphan — confirms the skill gate in loop.py
    # is the correct place to suppress it for non-committee agents.
    assert result is not None
    assert result[0] == "ok"


def test_freeze_ok_with_send_message_to_wrong_target_still_synthesizes():
    """send_message went to a peer, not the leader — still orphaned."""
    assistant_text = "[FREEZE OK]\n"
    turn_tool_info = _turn_tool_info_with_send_message_to_wrong_target()

    result = detect_orphaned_freeze(assistant_text, turn_tool_info, LEADER_ID)

    assert result is not None
    assert result[0] == "ok"


def test_last_freeze_line_wins_when_multiple_present():
    """When multiple FREEZE lines appear, the last one is authoritative."""
    assistant_text = (
        "[FREEZE NACK]: initial hesitation\n"
        "Actually, on reflection everything is stable.\n"
        "[FREEZE OK]\n"
    )
    turn_tool_info = _turn_tool_info_with_report_status()

    result = detect_orphaned_freeze(assistant_text, turn_tool_info, LEADER_ID)

    assert result is not None
    freeze_kind, _ = result
    assert freeze_kind == "ok"


def test_no_synthesis_when_assistant_text_is_none():
    """None assistant_text is a silent no-op."""
    result = detect_orphaned_freeze(None, {}, LEADER_ID)
    assert result is None


def test_no_synthesis_when_no_freeze_in_text():
    """No FREEZE pattern in text → no synthesis."""
    assistant_text = "All done, no freeze response needed."
    result = detect_orphaned_freeze(assistant_text, {}, LEADER_ID)
    assert result is None


def test_all_v2_committee_skills_in_constant():
    """Spot-check the expected v2 committee membership."""
    expected = {
        "product_manager_v2",
        "software_architect_v2",
        "ui_ux_designer_v2",
        "test_engineer_v2",
        "qa_engineer_v2",
        "engineer_advisor",
    }
    assert expected == V2_DESIGN_COMMITTEE_SKILLS
