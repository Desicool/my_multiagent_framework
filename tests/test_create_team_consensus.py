"""Tests for the create_team duplicate-description guardrail and consensus flag.

Mirrors the style of test_primitives_core.py: plain ``asyncio.run`` functions,
``FakeOrchestrator`` from the same module, no pytest-asyncio dependency.
"""

from __future__ import annotations

import asyncio

import pytest

from beidou.primitives.core import PrimitiveError, create_team

# Re-use the FakeOrchestrator and helpers from the existing primitive tests.
from test_primitives_core import FakeOrchestrator, _build


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Duplicate-description guard: N>1 identical (skill, description) tuples
# ---------------------------------------------------------------------------


def test_duplicate_descriptions_rejected_without_consensus():
    """N>1 members with identical (skill, description) raises duplicate_member_descriptions."""
    o = _build()
    roles = [
        {"role": "eng-1", "skill": "junior_engineer", "description": "Implement the whole API"},
        {"role": "eng-2", "skill": "junior_engineer", "description": "Implement the whole API"},
    ]

    async def body():
        with pytest.raises(PrimitiveError) as ei:
            await create_team(o, caller_id="R", name="impl", task="build", roles=roles)
        assert ei.value.code == "duplicate_member_descriptions"

    run(body())


def test_duplicate_descriptions_with_consensus_succeeds():
    """Same call with consensus=True bypasses the guard and succeeds."""
    o = _build()
    roles = [
        {"role": "attempt-1", "skill": "junior_engineer", "description": "Try to solve the puzzle"},
        {"role": "attempt-2", "skill": "junior_engineer", "description": "Try to solve the puzzle"},
    ]

    async def body():
        out = await create_team(
            o, caller_id="R", name="voting", task="ensemble", roles=roles, consensus=True
        )
        assert "team_id" in out
        assert len(out["members"]) == 2

    run(body())


def test_distinct_descriptions_succeed_without_consensus():
    """N>1 members with differentiated descriptions must not trigger the guard."""
    o = _build()
    roles = [
        {"role": "tester", "skill": "test_engineer", "description": "Run the test suite"},
        {"role": "deployer", "skill": "deployment_engineer", "description": "Write deploy.md"},
    ]

    async def body():
        out = await create_team(o, caller_id="R", name="qa-deploy", task="phase4", roles=roles)
        assert "team_id" in out
        assert len(out["members"]) == 2

    run(body())


def test_single_member_not_flagged():
    """N=1 should never trigger the guard regardless of description."""
    o = _build()
    roles = [
        {"role": "pm", "skill": "product_manager", "description": "Gather requirements"},
    ]

    async def body():
        out = await create_team(
            o, caller_id="R", name="requirements", task="phase1", roles=roles
        )
        assert "team_id" in out
        assert len(out["members"]) == 1

    run(body())


def test_same_description_different_skills_not_flagged():
    """If skills differ but descriptions match, it is intentional differentiation — no error."""
    o = _build()
    roles = [
        {"role": "r1", "skill": "test_engineer", "description": "Review the code"},
        {"role": "r2", "skill": "qa_engineer", "description": "Review the code"},
    ]

    async def body():
        out = await create_team(o, caller_id="R", name="review", task="review-task", roles=roles)
        assert "team_id" in out
        assert len(out["members"]) == 2

    run(body())


def test_three_members_all_identical_rejected():
    """Guard fires for N=3 all-identical members too, not just N=2."""
    o = _build()
    roles = [
        {"role": "a", "skill": "junior_engineer", "description": "Do everything"},
        {"role": "b", "skill": "junior_engineer", "description": "Do everything"},
        {"role": "c", "skill": "junior_engineer", "description": "Do everything"},
    ]

    async def body():
        with pytest.raises(PrimitiveError) as ei:
            await create_team(o, caller_id="R", name="bad", task="x", roles=roles)
        assert ei.value.code == "duplicate_member_descriptions"

    run(body())


def test_three_members_all_identical_with_consensus_succeeds():
    """consensus=True passes even for N=3 identical members."""
    o = _build()
    roles = [
        {"role": "a", "skill": "junior_engineer", "description": "Do everything"},
        {"role": "b", "skill": "junior_engineer", "description": "Do everything"},
        {"role": "c", "skill": "junior_engineer", "description": "Do everything"},
    ]

    async def body():
        out = await create_team(
            o, caller_id="R", name="ensemble", task="x", roles=roles, consensus=True
        )
        assert len(out["members"]) == 3

    run(body())


def test_roles_missing_description_both_none_flagged():
    """Two roles with no 'description' key both collapse to (skill, None).
    That is still a footgun (both members have no distinct task), so the
    guard fires."""
    o = _build()
    roles = [
        {"role": "r1", "skill": "junior_engineer"},
        {"role": "r2", "skill": "junior_engineer"},
    ]

    async def body():
        with pytest.raises(PrimitiveError) as ei:
            await create_team(o, caller_id="R", name="nodesc", task="x", roles=roles)
        assert ei.value.code == "duplicate_member_descriptions"

    run(body())
