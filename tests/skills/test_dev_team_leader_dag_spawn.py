"""Regression guard: dev_team_leader DAG-first dispatch contract.

Plan reference: tsk-f54d3beb (Bundle B / merge plan, issue #2 — DAG-first
dispatch). This test pins the merged dev_team_leader SKILL.md to the
contract Bundle A landed (commit 7d3eea1):

* parse `Runs_before` from each `tasks.md` row;
* declare ONE plan with `depends_on` edges (NOT a flat fan-out list);
* wave-spawn via `list_ready()` after every approval;
* final `integrator` plan entry whose `depends_on` covers every worker;
* `MISSING_OUTPUT` integrator failures escalate to the coordinator
  (no local rework — the eager-terminate flow leaves no rework target).

The merge folded the standalone `junior_engineer_v2` impl-leader into
`dev_team_leader`. The SKILL must NOT reference the deleted skill name and
must NOT carry the old hold-for-convergence wording (the merged contract
uses eager terminate per worker instead).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from beidou.skills.loader import load_skill


_SKILLS_ROOT = Path(__file__).resolve().parents[2] / "beidou" / "skills"
_DTL_SKILL = _SKILLS_ROOT / "coding_v2" / "dev_team_leader" / "SKILL.md"


# ---------------------------------------------------------------------------
# Structural sanity
# ---------------------------------------------------------------------------


def test_dev_team_leader_skill_file_exists():
    """Merged SKILL.md is on disk at the expected path."""
    assert _DTL_SKILL.is_file(), f"missing skill: {_DTL_SKILL}"


def test_dev_team_leader_loads_with_expected_name():
    """Loader resolves dev_team_leader to this SKILL.md (no DuplicateSkill)."""
    loaded = load_skill(_SKILLS_ROOT, "dev_team_leader")
    assert loaded.name == "dev_team_leader"
    assert loaded.source_path.resolve() == _DTL_SKILL.resolve()


# ---------------------------------------------------------------------------
# DAG-first dispatch contract
# ---------------------------------------------------------------------------


def test_skill_teaches_runs_before_translation():
    """SKILL must explain that `Runs_before` (the tasks.md field) translates
    into `depends_on` edges in the declared plan. This is the core DAG-first
    contract the architect → leader handshake relies on.
    """
    body = _DTL_SKILL.read_text()
    assert "Runs_before" in body, (
        "dev_team_leader SKILL must reference the architect's Runs_before field"
    )
    # The translation note must be explicit somewhere in the prose.
    assert "= Runs_before" in body or "(semantically `depends_on`)" in body, (
        "SKILL must explicitly map Runs_before -> depends_on so the leader "
        "does not silently drop dependency edges"
    )


def test_declare_plan_template_uses_depends_on_not_flat_list():
    """The declare_plan template must show `depends_on` per task entry, NOT a
    flat fan-out (which is the tsk_d1ac5a39 fail-mode the merged contract
    explicitly forbids).
    """
    body = _DTL_SKILL.read_text()
    assert "depends_on" in body, (
        "declare_plan template must declare per-task depends_on edges"
    )
    # Defense: the SKILL must explicitly call out flat-fan-out as a fail-mode.
    assert "Flat fan-out" in body or "flat fan-out" in body, (
        "SKILL must name flat fan-out as a forbidden anti-pattern"
    )
    assert "tsk_d1ac5a39" in body, (
        "SKILL must cite the originating fail-mode (tsk_d1ac5a39)"
    )


def test_skill_uses_list_ready_for_wave_dispatch():
    """Wave-based spawn is gated by `list_ready()`. Without this, the leader
    cannot know which downstream tasks are unblocked after a terminate_child.
    """
    body = _DTL_SKILL.read_text()
    assert "list_ready" in body, (
        "dev_team_leader must use list_ready() to discover newly-ready tasks"
    )
    # Also assert it appears in `allowed-tools` frontmatter so the agent can call it.
    assert "  - list_ready" in body, (
        "list_ready must be in the SKILL's allowed-tools frontmatter"
    )


def test_integrator_is_final_node_with_full_depends_on():
    """Integrator is the final plan node and its depends_on must cover every
    worker, so it only runs after the entire wave graph has been approved.
    The SKILL's example must show this shape.
    """
    body = _DTL_SKILL.read_text()
    # Locate the integrator plan-entry in the declare_plan example.
    assert 'id: "integrator"' in body, (
        "declare_plan template must include an integrator entry"
    )
    # The integrator entry must declare depends_on with at least one worker id
    # (the example uses task-deps and task-1) — same-line / same-block.
    integ_idx = body.find('id: "integrator"')
    integ_block = body[integ_idx : integ_idx + 600]
    assert "depends_on" in integ_block, (
        "integrator plan entry must declare depends_on (its depends_on lists "
        "every worker so it only runs after all workers terminate_child)"
    )
    assert '"task-' in integ_block, (
        "integrator depends_on must reference the worker task ids"
    )
    # Prose must also state the contract for human reviewers.
    assert "depends_on lists every worker" in body or "depends_on = [every worker id" in body, (
        "SKILL must state that integrator.depends_on covers every worker"
    )


def test_missing_output_escalates_to_coordinator():
    """MISSING_OUTPUT (integrator-detected implementer failure) escalates to
    the coordinator via `[INT-CONFLICT] subtype=missing_output`. The leader
    does NOT locally rework the failed task — under eager-terminate the
    upstream worker is already gone and there is no rework target.
    """
    body = _DTL_SKILL.read_text()
    assert "MISSING_OUTPUT" in body, "SKILL must document the missing-output failure mode"
    assert "[INT-CONFLICT]" in body, "SKILL must use the [INT-CONFLICT] envelope upstream"
    # The subtype tag and the envelope must appear in the same handling section
    # (within ~400 chars of each other) so we know the routing is correct.
    mo_idx = body.find("MISSING_OUTPUT")
    handling_block = body[mo_idx : mo_idx + 800]
    assert "subtype=missing_output" in handling_block, (
        "MISSING_OUTPUT handling block must emit subtype=missing_output to the coordinator"
    )
    assert "[INT-CONFLICT]" in handling_block, (
        "MISSING_OUTPUT handling block must use the [INT-CONFLICT] envelope"
    )
    # Defensive: forbid the old "send rework back to impl-leader" pattern.
    assert "rework back to impl-leader" not in body
    assert "send to impl-leader" not in body


# ---------------------------------------------------------------------------
# Merge cleanup — the standalone junior_engineer_v2 skill is gone.
# ---------------------------------------------------------------------------


def test_skill_does_not_reference_deleted_junior_engineer_v2():
    """Post-merge, `junior_engineer_v2` does not exist as a skill (commit
    0697f53 removed coding_v2/junior_engineer/SKILL.md and the v2 fork was
    never re-introduced). Spawning that name would raise SkillNotFound.
    """
    body = _DTL_SKILL.read_text()
    assert "junior_engineer_v2" not in body, (
        "dev_team_leader must not spawn junior_engineer_v2 — that skill was "
        "folded into this one (plan tsk-f54d3beb, commits 0697f53 / 7d3eea1)"
    )


def test_skill_does_not_use_hold_for_convergence_wording():
    """The merged contract uses **eager terminate per worker** (advances the
    plan task, unblocks dependents). The pre-merge hold-for-convergence
    pattern (where children stayed alive in review_pending until the leader's
    own upstream review resolved) is no longer the model.
    """
    body = _DTL_SKILL.read_text()
    lower = body.lower()
    assert "hold for convergence" not in lower, (
        "merged dev_team_leader uses eager terminate, not hold-for-convergence"
    )
    assert "hold for upstream" not in lower, (
        "merged dev_team_leader does not hold children pending its own upstream review"
    )
    # Positive: the eager-terminate model must be documented.
    assert "eager terminate" in lower or "eager-terminate" in lower, (
        "SKILL must document the eager-terminate-per-worker model"
    )
