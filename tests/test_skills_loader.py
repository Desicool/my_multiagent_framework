"""Tests for the new SDK-agent skill loader API."""
from __future__ import annotations

from pathlib import Path

import pytest

from beidou.skills.loader import (
    DuplicateSkill,
    InvalidSkillFile,
    LoadedSkill,
    SkillNotFound,
    build_system_prompt,
    load_skill,
    load_skill_file,
    provision_skills,
    render_system_prompt,
    sdk_builtins_allowlist,
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
    rendered = render_system_prompt(skill, workspace_path="/tmp/foo", project_workspace_path="/tmp/proj")

    assert "/tmp/foo" in rendered
    assert "/tmp/proj" in rendered
    # Placeholder literals must be gone.
    assert "{workspace_path}" not in rendered
    assert "{project_workspace_path}" not in rendered
    # Unknown-looking tokens that happen to share brace syntax MUST survive.
    # The rewritten orchestrator body contains `{id: "pm", ...}` declare_plan
    # task spec examples and nothing we pass should consume them.
    assert '{id: "pm",' in rendered


def test_render_leaves_unknown_placeholder_literal() -> None:
    skill = LoadedSkill(
        name="t",
        version="1",
        description="",
        allowed_tools=[],
        system_prompt="hello {role}, workspace={workspace_path}, unknown={something_else}",
    )
    rendered = render_system_prompt(skill, role="pm", workspace_path="/w", project_workspace_path="/proj")

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


# ---------------------------------------------------------------------------
# Tests for provision_skills
# ---------------------------------------------------------------------------


def test_provision_skills_writes_canonical_copies(tmp_path: Path) -> None:
    """provision_skills copies each bundled SKILL.md byte-for-byte (no substitution)."""
    written = provision_skills(tmp_path, skill_root=SKILLS_ROOT)

    # At least some files should have been written (we have bundled skills).
    assert len(written) > 0

    # Each written file should exist and be byte-identical to its source SKILL.md.
    for dst in written:
        assert dst.exists(), f"Expected {dst} to exist"
        # Determine which source SKILL.md corresponds to this destination.
        # dst is <workspace>/.claude/skills/<name>/SKILL.md
        skill_name = dst.parent.name
        # Find the source by scanning SKILLS_ROOT for a SKILL.md with that name.
        source = None
        for src_md in SKILLS_ROOT.rglob("SKILL.md"):
            src_text = src_md.read_bytes()
            src_loaded = load_skill_file(src_md)
            if src_loaded.name == skill_name:
                source = src_md
                source_bytes = src_text
                break
        assert source is not None, f"Could not find source for skill name {skill_name!r}"
        assert dst.read_bytes() == source_bytes, (
            f"Destination {dst} is not byte-identical to source {source}"
        )

        # Confirm NO substitution occurred: if the source has {workspace_path},
        # so should the destination.
        if b"{workspace_path}" in source_bytes:
            assert b"{workspace_path}" in dst.read_bytes(), (
                f"Substitution was applied on disk — it should NOT be"
            )


def test_provision_skills_idempotent(tmp_path: Path) -> None:
    """Calling provision_skills twice: second call returns [] (no files re-written)."""
    first = provision_skills(tmp_path, skill_root=SKILLS_ROOT)
    assert len(first) > 0  # first call wrote something

    second = provision_skills(tmp_path, skill_root=SKILLS_ROOT)
    assert second == [], (
        f"Second call should return [] (all files already up-to-date), got {second}"
    )

    # Verify the files are still present and unchanged.
    for dst in first:
        assert dst.exists()


def test_provision_skills_replaces_modified_destination(tmp_path: Path) -> None:
    """If a destination has been tampered with, provision_skills restores the canonical content."""
    # First provision — establish the canonical copies.
    first = provision_skills(tmp_path, skill_root=SKILLS_ROOT)
    assert len(first) > 0

    # Overwrite one destination with bogus content.
    target = first[0]
    original_bytes = target.read_bytes()
    target.write_bytes(b"corrupted content, definitely wrong")

    # Second provision — should detect the mismatch and restore.
    second = provision_skills(tmp_path, skill_root=SKILLS_ROOT)

    assert target in second, (
        f"Expected {target} to be re-written on second call, but it wasn't in {second}"
    )
    assert target.read_bytes() == original_bytes, (
        "File was not restored to canonical content"
    )


def test_provision_skills_nonexistent_skill_root(tmp_path: Path) -> None:
    """provision_skills returns [] when skill_root does not exist."""
    nonexistent = tmp_path / "does_not_exist"
    result = provision_skills(tmp_path / "workspace", skill_root=nonexistent)
    assert result == []


# ---------------------------------------------------------------------------
# Tests for build_system_prompt
# ---------------------------------------------------------------------------


def _make_fake_skill(body: str = "role={role}, ws={workspace_path}") -> LoadedSkill:
    """Build a minimal LoadedSkill for testing."""
    return LoadedSkill(
        name="test_skill",
        version="2.0.0",
        description="A test skill",
        allowed_tools=[],
        system_prompt=body,
    )


def _make_spawn_ctx(**kwargs) -> dict:
    defaults = {
        "role": "engineer",
        "role_description": "writes code",
        "team_name": "alpha",
        "workspace_path": "/tmp/ws",
        "project_workspace_path": "/tmp/proj",
        "leader_id": "agent-leader-123",
    }
    defaults.update(kwargs)
    return defaults


def test_build_system_prompt_skill_body_first() -> None:
    """Output starts with [ASSIGNED SKILL] marker; skill name/version in BEGIN SKILL line;
    {role} inside skill body gets substituted; IDENTITY block has role/team/workspace/leader;
    CONTRACT block has required phrases; OTHER SKILLS is at the end."""
    skill = _make_fake_skill("role={role}, ws={workspace_path}")
    ctx = _make_spawn_ctx(role="pm", workspace_path="/workspace/alpha", project_workspace_path="/workspace", team_name="team-one", leader_id="boss-99")
    prompt = build_system_prompt(skill, ctx)

    # Must start with the ASSIGNED SKILL section.
    assert prompt.startswith("[ASSIGNED SKILL")

    # BEGIN SKILL marker contains name and version.
    assert "──── BEGIN SKILL: test_skill v2.0.0 ────" in prompt

    # {role} substitution inside skill body.
    assert "role=pm" in prompt
    assert "ws=/workspace/alpha" in prompt

    # IDENTITY block fields.
    assert "You are pm in team team-one." in prompt
    assert "Workspace: /workspace/alpha." in prompt
    assert "Project workspace: /workspace." in prompt
    assert "Leader: boss-99." in prompt

    # CONTRACT block required phrases.
    assert "Completion is a state" in prompt
    assert "completion handoff" in prompt

    # OTHER SKILLS at the end.
    assert prompt.rstrip().endswith(
        "authoritative for your role and approach."
    )

    # Section order: SKILL → IDENTITY → CONTRACT → OTHER SKILLS.
    idx_skill = prompt.index("[ASSIGNED SKILL")
    idx_identity = prompt.index("[IDENTITY]")
    idx_contract = prompt.index("[PERSISTENT-AGENT CONTRACT]")
    idx_other = prompt.index("[OTHER SKILLS]")
    assert idx_skill < idx_identity < idx_contract < idx_other


def test_build_system_prompt_cache_prefix_invariant() -> None:
    """Two different spawn contexts with the same skill and no placeholders in the
    skill body produce byte-identical prefixes up to the END SKILL marker."""
    # Use a skill body with NO substitution placeholders so both contexts
    # produce the same substituted body.
    skill = _make_fake_skill("This skill body has no placeholders at all.")
    ctx1 = _make_spawn_ctx(role="alice", team_name="team-a", workspace_path="/ws/a", leader_id="boss-1")
    ctx2 = _make_spawn_ctx(role="bob", team_name="team-b", workspace_path="/ws/b", leader_id="boss-2")

    prompt1 = build_system_prompt(skill, ctx1)
    prompt2 = build_system_prompt(skill, ctx2)

    marker = "──── END SKILL ────"
    end1 = prompt1.index(marker) + len(marker)
    end2 = prompt2.index(marker) + len(marker)

    prefix1 = prompt1[:end1]
    prefix2 = prompt2[:end2]

    assert prefix1 == prefix2, (
        "Cache prefix (up to END SKILL) differs across spawn contexts using the same skill.\n"
        f"prefix1: {prefix1!r}\nprefix2: {prefix2!r}"
    )


def test_build_system_prompt_missing_leader_id() -> None:
    """spawn_ctx without leader_id renders 'Leader: unset' without crashing."""
    skill = _make_fake_skill("no placeholders here")
    ctx = _make_spawn_ctx()
    del ctx["leader_id"]  # remove leader_id entirely

    prompt = build_system_prompt(skill, ctx)
    assert "Leader: unset." in prompt


# ---------------------------------------------------------------------------
# Tests for sdk_builtins_allowlist
# ---------------------------------------------------------------------------


def test_sdk_builtins_allowlist_basic() -> None:
    """Beidou primitives like create_team are filtered; bash->Bash, file_read->Read."""
    result = sdk_builtins_allowlist(["bash", "file_read", "create_team"])
    assert result == ["Bash", "Read"]


def test_sdk_builtins_allowlist_dedup_and_order() -> None:
    """Duplicates are deduplicated preserving first-occurrence order; unknowns dropped."""
    result = sdk_builtins_allowlist([
        "bash",
        "file_read",
        "bash",         # duplicate
        "unknown_tool", # not in map
        "file_write",
        "web_search",
        "web_fetch",
        "send_message", # Beidou MCP primitive — filtered
        "web_fetch",    # duplicate
    ])
    assert result == ["Bash", "Read", "Write", "WebSearch", "WebFetch"]


# ---------------------------------------------------------------------------
# New tests for project_workspace_path support
# ---------------------------------------------------------------------------


def test_project_workspace_path_substituted_in_skill_body() -> None:
    """render_system_prompt replaces {project_workspace_path} inside the skill body."""
    skill = LoadedSkill(
        name="t",
        version="1",
        description="",
        allowed_tools=[],
        system_prompt="team_ws={workspace_path}, proj_ws={project_workspace_path}",
    )
    rendered = render_system_prompt(
        skill,
        workspace_path="/team/ws",
        project_workspace_path="/proj/ws",
    )
    assert rendered == "team_ws=/team/ws, proj_ws=/proj/ws"
    assert "{project_workspace_path}" not in rendered


def test_identity_block_contains_both_workspace_and_project_workspace() -> None:
    """IDENTITY block has both Workspace and Project workspace lines in correct order."""
    skill = _make_fake_skill("no placeholders here")
    ctx = _make_spawn_ctx(
        workspace_path="/team/workspace",
        project_workspace_path="/project/root",
        leader_id="leader-42",
    )
    prompt = build_system_prompt(skill, ctx)

    identity_start = prompt.index("[IDENTITY]")
    contract_start = prompt.index("[PERSISTENT-AGENT CONTRACT]")
    identity_block = prompt[identity_start:contract_start]

    assert "Workspace: /team/workspace." in identity_block
    assert "Project workspace: /project/root." in identity_block
    assert "Leader: leader-42." in identity_block

    ws_idx = identity_block.index("Workspace: /team/workspace.")
    proj_idx = identity_block.index("Project workspace: /project/root.")
    leader_idx = identity_block.index("Leader: leader-42.")
    assert ws_idx < proj_idx < leader_idx


def test_provision_skills_writes_to_caller_supplied_dir(tmp_path: Path) -> None:
    """provision_skills writes SKILL.md files into <dir>/.claude/skills/<name>/SKILL.md."""
    target = tmp_path / "agent_workspace"
    written = provision_skills(target, skill_root=SKILLS_ROOT)

    assert len(written) > 0
    for dst in written:
        assert dst.name == "SKILL.md"
        assert dst.parent.parent.name == "skills"
        assert dst.parent.parent.parent.name == ".claude"
        assert dst.parent.parent.parent.parent == target
