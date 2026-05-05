"""Fork verification: coding_v2/junior_engineer (bd issue 3jey).

Confirms that the new impl-leader skill loads cleanly under the unique
name ``junior_engineer_v2`` and carries the persona-level guards that
distinguish it from the v1 ``coding/junior_engineer`` it forks:

* NEVER-DO ``TodoWrite`` (defense-in-depth on top of SDK disallow)
* explicit completion checklist after the final ``terminate_child``
  (closes the bug pattern from ``tsk_658f44b6``)
* orchestrator_v2's "impl" task spawns ``junior_engineer_v2`` (the
  leader), not ``junior_engineer`` (which remains the leaf worker)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from beidou.skills.loader import (
    DuplicateSkill,
    SkillNotFound,
    load_skill,
    load_skill_file,
)


_SKILLS_ROOT = Path(__file__).resolve().parents[2] / "beidou" / "skills"
_V2_SKILL = _SKILLS_ROOT / "coding_v2" / "junior_engineer" / "SKILL.md"
_V1_SKILL = _SKILLS_ROOT / "coding" / "junior_engineer" / "SKILL.md"
_ORCH_V2 = _SKILLS_ROOT / "coding_v2" / "orchestrator" / "SKILL.md"


def test_v2_skill_file_exists():
    """Fork artifact is on disk."""
    assert _V2_SKILL.is_file(), f"missing fork: {_V2_SKILL}"


def test_v2_skill_loads_with_unique_name():
    """Loader resolves junior_engineer_v2 to exactly one SKILL.md, no DuplicateSkill."""
    loaded = load_skill(_SKILLS_ROOT, "junior_engineer_v2")
    assert loaded.name == "junior_engineer_v2"
    assert loaded.source_path.resolve() == _V2_SKILL.resolve()


def test_v1_skill_still_loads_unchanged_name():
    """v1 sibling remains under its original name; forking did not collide."""
    loaded = load_skill(_SKILLS_ROOT, "junior_engineer")
    assert loaded.name == "junior_engineer"
    assert loaded.source_path.resolve() == _V1_SKILL.resolve()


def test_v2_body_forbids_todowrite():
    """Defense-in-depth NEVER-DO bullet against TodoWrite is present."""
    body = _V2_SKILL.read_text()
    assert "NEVER call `TodoWrite`" in body, (
        "junior_engineer_v2 must declare a NEVER-DO against TodoWrite"
    )


def test_v2_body_has_post_terminate_completion_checklist():
    """The contract that closes tsk_658f44b6: emit envelope + signal_review after final terminate_child."""
    body = _V2_SKILL.read_text()
    # Markers that together prove the checklist exists in the body.
    assert "After the LAST child terminates" in body
    assert "[REVIEW REQUIRED]" in body
    assert 'signal_review(' in body
    assert 'detail="' in body
    assert "tsk_658f44b6" in body, "rationale link to the originating bug should be present"


def test_v2_body_forbids_writing_code_directly():
    """Persona inverts v1's 'default solo' — leader never writes code itself."""
    body = _V2_SKILL.read_text()
    assert "NEVER write code yourself" in body


def test_dev_team_leader_spawns_junior_engineer_v2_for_impl_task():
    """dev_team_leader's plan declares skill=junior_engineer_v2 for impl tasks."""
    dev_lead = _SKILLS_ROOT / "coding_v2" / "dev_team_leader" / "SKILL.md"
    body = dev_lead.read_text()
    assert 'skill: "junior_engineer_v2"' in body, (
        "dev_team_leader should spawn junior_engineer_v2 (leader) per iteration"
    )


def test_orchestrator_v2_allowed_skills_includes_coordinators():
    """orchestrator_v2's frontmatter skills: list includes phase coordinators."""
    body = _ORCH_V2.read_text()
    assert "  - phase1_coordinator\n" in body
    assert "  - phase2_coordinator\n" in body
    # orchestrator no longer references v1 or v2 leaf skills directly.
    assert "  - junior_engineer_v2\n" not in body, (
        "orchestrator_v2 delegates to coordinators; it no longer lists "
        "junior_engineer_v2 directly"
    )
