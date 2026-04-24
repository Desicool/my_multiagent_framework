"""Tests for the new SDK-agent skill loader API."""
from __future__ import annotations

from pathlib import Path

import pytest

from beidou.skills.loader import (
    DuplicateSkill,
    InvalidSkillFile,
    LoadedSkill,
    SkillNotFound,
    load_skill,
    load_skill_file,
    render_system_prompt,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "beidou" / "skills"
ORCHESTRATOR_PATH = SKILLS_ROOT / "coding" / "orchestrator" / "SKILL.md"


def test_load_orchestrator_by_path() -> None:
    skill = load_skill_file(ORCHESTRATOR_PATH)

    assert isinstance(skill, LoadedSkill)
    assert skill.name == "orchestrator"
    assert skill.version  # non-empty
    assert skill.description  # non-empty
    assert skill.source_path == ORCHESTRATOR_PATH

    # create_team is a Beidou MCP primitive: it must be namespaced.
    assert "mcp__beidou__create_team" in skill.allowed_tools
    # Legacy names (bash, file_read, file_write) are mapped to SDK built-in names.
    assert "Bash" in skill.allowed_tools
    assert "Read" in skill.allowed_tools
    assert "Write" in skill.allowed_tools

    # Sub-skills survive verbatim.
    assert "junior_engineer" in skill.sub_skills
    assert "product_manager" in skill.sub_skills


def test_load_orchestrator_by_name() -> None:
    skill = load_skill(SKILLS_ROOT, "orchestrator")
    by_path = load_skill_file(ORCHESTRATOR_PATH)

    assert skill.name == by_path.name
    assert skill.version == by_path.version
    assert skill.allowed_tools == by_path.allowed_tools
    assert skill.system_prompt == by_path.system_prompt
    assert skill.source_path == ORCHESTRATOR_PATH


def test_render_system_prompt_substitutes_known_and_preserves_unknown() -> None:
    skill = load_skill_file(ORCHESTRATOR_PATH)
    rendered = render_system_prompt(skill, workspace_path="/tmp/foo")

    assert "/tmp/foo" in rendered
    # Placeholder literal must be gone.
    assert "{workspace_path}" not in rendered
    # Unknown-looking tokens that happen to share brace syntax MUST survive.
    # The orchestrator body contains `{role: "<task-id>", ...}` JSON-ish and
    # nothing we pass should replace them.
    assert '{role: "<task-id>"' in rendered


def test_render_leaves_unknown_placeholder_literal() -> None:
    skill = LoadedSkill(
        name="t",
        version="1",
        description="",
        allowed_tools=[],
        system_prompt="hello {role}, workspace={workspace_path}, unknown={something_else}",
    )
    rendered = render_system_prompt(skill, role="pm", workspace_path="/w")

    assert rendered == "hello pm, workspace=/w, unknown={something_else}"


def test_skill_not_found() -> None:
    with pytest.raises(SkillNotFound):
        load_skill(SKILLS_ROOT, "definitely_not_a_real_skill_name_xyz")


def test_invalid_skill_file_no_frontmatter(tmp_path: Path) -> None:
    bad = tmp_path / "SKILL.md"
    bad.write_text("just a body, no frontmatter at all\n")

    with pytest.raises(InvalidSkillFile):
        load_skill_file(bad)


def test_invalid_skill_file_missing_name(tmp_path: Path) -> None:
    bad = tmp_path / "SKILL.md"
    bad.write_text("---\nversion: 1.0.0\n---\nbody\n")

    with pytest.raises(InvalidSkillFile):
        load_skill_file(bad)


def test_allowed_tools_underscore_synonym_accepted(tmp_path: Path) -> None:
    p = tmp_path / "SKILL.md"
    p.write_text(
        "---\n"
        "name: syn\n"
        "version: 1.0.0\n"
        "description: test\n"
        "allowed_tools:\n"
        "  - send_message\n"
        "  - bash\n"
        "---\n"
        "body\n"
    )
    skill = load_skill_file(p)
    # send_message -> mcp__beidou__send_message (Beidou MCP primitive)
    # bash -> Bash (SDK built-in tool mapping)
    assert skill.allowed_tools == ["mcp__beidou__send_message", "Bash"]


def test_duplicate_skill_detected(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    body = (
        "---\n"
        "name: dup\n"
        "version: 1.0.0\n"
        "description: test\n"
        "---\n"
        "body\n"
    )
    (a / "SKILL.md").write_text(body)
    (b / "SKILL.md").write_text(body)

    with pytest.raises(DuplicateSkill):
        load_skill(tmp_path, "dup")


def test_junior_engineer_loads_with_unknown_model_field() -> None:
    """junior_engineer's frontmatter has a `model:` field not in the spec;
    unknown keys must be preserved-and-ignored, not cause InvalidSkillFile."""
    path = SKILLS_ROOT / "coding" / "junior_engineer" / "SKILL.md"
    skill = load_skill_file(path)
    assert skill.name == "junior_engineer"
