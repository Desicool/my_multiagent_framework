"""Regression guard: v2 design-committee SKILLs declare signal_review preconditions.

Plan reference: bd issue `my_simple_agent-7k02`. Fail-mode anchor: `tsk_4961b649`
— test_engineer (ag_43d480a5) called `signal_review` at +107s, after writing the
initial `test_plan.md` draft, BEFORE any peer replied to its 11 outgoing
testability-gap inquiries. The round was effectively wasted because
`test_plan.md` had not incorporated PM/architect/UI-UX answers.

Other 5 committee members did not reproduce the bug in that trace, but the
SKILL prompt is structurally identical across all six (linear N-step
"Workflow at a glance" ending with "Submit for review") — the bug is LLM
variance, not robust design.

The fix this test pins:

1. "Workflow at a glance" must declare an explicit multi-round loop using the
   marker phrase `Loop UNTIL` (so the LLM sees it as iteration, not a recipe).
2. A new dedicated section `Preconditions for calling signal_review` must
   appear in each SKILL.md, listing the three hard rules (outgoing inquiries
   replied or noted pending; incoming critiques responded to; deliverable
   stable in turn).
3. The fail-mode tag `tsk_4961b649` must appear in each SKILL.md as a
   grep-able anchor for future archaeology.
4. The precondition refers to the `Open questions / risks` envelope field —
   this asserts the new prose stays consistent with the existing
   `[REVIEW REQUIRED]` envelope schema (which all six SKILLs already define).

Six v2 design-committee SKILLs are guarded:
  - product_manager (writes requirements.md)
  - software_architect (writes spec.md)
  - ui_ux_designer (writes ui_ux.md)
  - test_engineer (writes test_plan.md)
  - qa_engineer (writes qa_plan.md)
  - engineer_advisor (writes impl_plan.md)
"""
from __future__ import annotations

from pathlib import Path

import pytest


_SKILLS_ROOT = Path(__file__).resolve().parents[2] / "beidou" / "skills" / "coding_v2"

_COMMITTEE_SKILLS = {
    "product_manager": _SKILLS_ROOT / "product_manager" / "SKILL.md",
    "software_architect": _SKILLS_ROOT / "software_architect" / "SKILL.md",
    "ui_ux_designer": _SKILLS_ROOT / "ui_ux_designer" / "SKILL.md",
    "test_engineer": _SKILLS_ROOT / "test_engineer" / "SKILL.md",
    "qa_engineer": _SKILLS_ROOT / "qa_engineer" / "SKILL.md",
    "engineer_advisor": _SKILLS_ROOT / "engineer_advisor" / "SKILL.md",
}


def _read(role: str) -> str:
    path = _COMMITTEE_SKILLS[role]
    assert path.is_file(), f"missing v2 committee SKILL.md: {path}"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Per-role assertions — one test function per (role, assertion) pair so
# failures localize cleanly instead of one mega-test failing on the first
# missing string.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", sorted(_COMMITTEE_SKILLS.keys()))
def test_v2_committee_skill_declares_signal_review_preconditions(role: str) -> None:
    """The new Preconditions section must exist by exact heading."""
    body = _read(role)
    assert "Preconditions for calling signal_review" in body, (
        f"{role} SKILL.md must declare an explicit "
        '"Preconditions for calling signal_review" section before the '
        "signal_review/[REVIEW REQUIRED] guidance — bd my_simple_agent-7k02 / "
        "tsk_4961b649 fix."
    )


@pytest.mark.parametrize("role", sorted(_COMMITTEE_SKILLS.keys()))
def test_v2_committee_skill_declares_multiround_loop(role: str) -> None:
    """Workflow at a glance must use the explicit `Loop UNTIL` marker so the
    LLM does not read step-4-and-then-submit as a single linear recipe.
    """
    body = _read(role)
    assert "Loop UNTIL" in body, (
        f"{role} SKILL.md 'Workflow at a glance' must use 'Loop UNTIL' to "
        "make the multi-round, multi-turn nature visible at a glance "
        "(bd my_simple_agent-7k02)."
    )


@pytest.mark.parametrize("role", sorted(_COMMITTEE_SKILLS.keys()))
def test_v2_committee_skill_cites_fail_mode_anchor(role: str) -> None:
    """`tsk_4961b649` must appear as a grep-able anchor referencing the
    canonical fail-mode trace, so future archaeology can rediscover this fix.
    """
    body = _read(role)
    assert "tsk_4961b649" in body, (
        f"{role} SKILL.md must reference tsk_4961b649 as the canonical "
        "fail-mode anchor for the signal_review-preconditions fix "
        "(bd my_simple_agent-7k02)."
    )


@pytest.mark.parametrize("role", sorted(_COMMITTEE_SKILLS.keys()))
def test_v2_committee_skill_precondition_uses_envelope_field_name(role: str) -> None:
    """Precondition #1 cites the `Open questions / risks` envelope line —
    this guards against drift between the new precondition prose and the
    existing [REVIEW REQUIRED] envelope schema.
    """
    body = _read(role)
    assert "Open questions / risks" in body, (
        f"{role} SKILL.md must reference the existing 'Open questions / risks' "
        "envelope field in the precondition prose (so 'pending peer answer' "
        "actually has a legal envelope slot to live in)."
    )
