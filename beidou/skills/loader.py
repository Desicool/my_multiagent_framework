"""Skill loading.

This module provides two layers:

1. The legacy API (``load_skills``, ``skills_as_tools``) kept for backward
   compatibility with ``beidou/team.py`` and ``beidou/skills/tool.py``.
   NOTE: ``skills_as_tools`` is dead code now that ``beidou/skills/tool.py``
   and ``beidou/team.py`` are retired; it will be removed in a follow-up.

2. The new SDK-agent API (``load_skill``, ``load_skill_file``,
   ``render_system_prompt``, ``LoadedSkill``) consumed by
   ``beidou/sdk_agent.py``. See ``docs/skills.md`` for the authoritative
   SKILL.md spec and ``docs/tool-surface.md`` for the MCP tool name
   namespacing rules applied here.

3. Per-team provisioning helpers (``provision_skills``) and per-agent-spawn
   system prompt assembly (``build_system_prompt``) and SDK builtin allowlist
   extraction (``sdk_builtins_allowlist``).
"""
from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from beidou.skills.base import Skill, parse_skill_md

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Legacy discovery API (unchanged behaviour — other modules depend on it)
# ---------------------------------------------------------------------------


def _bundled_skills_dir() -> Path:
    """Return the beidou/skills/ directory — works frozen (PyInstaller) and editable."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "beidou" / "skills"  # type: ignore[attr-defined]
    return Path(__file__).parent


def load_skills(extra_paths: list[Path] | None = None) -> dict[str, Skill]:
    """Scan skill directories in priority order; later entries override earlier ones.

    Order (lowest to highest priority):
      1. beidou/skills/           — bundled defaults shipped with the binary
      2. ~/.claude/skills/        — user-level skills shared with Claude Code
      3. {cwd}/.beidou/skills/    — project-local overrides
      4. extra_paths              — caller-supplied paths
    """
    search_paths: list[Path] = [
        _bundled_skills_dir(),
        Path.home() / ".claude" / "skills",
        Path.cwd() / ".beidou" / "skills",
        *(extra_paths or []),
    ]

    skills: dict[str, Skill] = {}
    for base in search_paths:
        if not base.is_dir():
            continue
        for skill_md in sorted(base.rglob("SKILL.md")):
            try:
                s = parse_skill_md(skill_md)
                skills[s.name] = s
            except Exception:
                pass  # skip malformed skills silently
    return skills


def skills_as_tools(skill_names: list[str], all_skills: dict[str, Skill]) -> list:
    """Deprecated — SkillTool has been retired with beidou/skills/tool.py.

    This function is kept as a no-op stub so that any stale callers don't
    raise AttributeError on import; it returns an empty list. It will be
    removed in a follow-up cleanup pass.
    """
    return []


# ---------------------------------------------------------------------------
# New SDK-agent API
# ---------------------------------------------------------------------------


# The Beidou MCP primitives from docs/tool-surface.md. Entries matching
# any of these are namespaced to ``mcp__beidou__<name>``; anything else is
# passed through verbatim for the sdk_agent layer to validate.
_BEIDOU_MCP_TOOLS: frozenset[str] = frozenset(
    {
        "send_message",
        "list_peers",
        "ask_user",
        "report_status",
        "create_team",
        "terminate_child",
    }
)

_MCP_PREFIX = "mcp__beidou__"

# Mapping from legacy Beidou tool names to SDK built-in canonical tool names.
# This is module-level so it can be reused by sdk_builtins_allowlist().
_SDK_BUILTIN_MAP: dict[str, str] = {
    "bash": "Bash",
    "file_read": "Read",
    "file_write": "Write",
    "web_search": "WebSearch",
    "web_fetch": "WebFetch",
}

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)


class SkillError(Exception):
    """Base class for all skill-loading errors."""


class SkillNotFound(SkillError):
    def __init__(self, name: str, skill_root: Path):
        super().__init__(f"No SKILL.md with name={name!r} under {skill_root}")
        self.name = name
        self.skill_root = skill_root


class DuplicateSkill(SkillError):
    def __init__(self, name: str, paths: list[Path]):
        super().__init__(
            f"Multiple SKILL.md files declare name={name!r}: "
            + ", ".join(str(p) for p in paths)
        )
        self.name = name
        self.paths = paths


class InvalidSkillFile(SkillError):
    """Malformed frontmatter, bad YAML, or missing required fields."""


@dataclass
class LoadedSkill:
    name: str
    version: str
    description: str
    allowed_tools: list[str]  # namespaced where applicable, legacy names passed through
    allowed_tools_raw: list[str] = field(default_factory=list)  # raw unnamespaced names from frontmatter
    sub_skills: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    system_prompt: str = ""  # body, un-substituted
    source_path: Path = field(default_factory=lambda: Path())


def _namespace_tool(name: str) -> str:
    """Map a bare tool name to its canonical form.

    Two namespacing layers apply in order:

    1. Beidou MCP primitives (``send_message``, ``create_team``, etc.) get the
       ``mcp__beidou__`` prefix because they are served by the per-spawn in-process
       MCP server (see ``docs/tool-surface.md``).

    2. SDK built-in tool names are mapped from their legacy beidou names to the
       claude-agent-sdk canonical names.  These are NOT ``mcp__beidou__``-prefixed —
       they are provided by the SDK directly:
           bash        -> Bash
           file_read   -> Read
           file_write  -> Write
           web_search  -> WebSearch
           web_fetch   -> WebFetch

    Anything not in either table is returned verbatim (forward-compatible with
    new SDK built-ins and for already-prefixed names).
    """
    if name in _BEIDOU_MCP_TOOLS:
        return _MCP_PREFIX + name
    return _SDK_BUILTIN_MAP.get(name, name)


def _parse_skill_text(text: str, source_path: Path) -> LoadedSkill:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise InvalidSkillFile(
            f"SKILL.md missing YAML frontmatter delimiters: {source_path}"
        )

    frontmatter_raw, body = match.group(1), match.group(2)
    try:
        fm = yaml.safe_load(frontmatter_raw) or {}
    except yaml.YAMLError as exc:
        raise InvalidSkillFile(
            f"SKILL.md frontmatter is not valid YAML: {source_path}: {exc}"
        ) from exc

    if not isinstance(fm, dict):
        raise InvalidSkillFile(
            f"SKILL.md frontmatter must be a mapping: {source_path}"
        )

    name = fm.get("name")
    if not name or not isinstance(name, str):
        raise InvalidSkillFile(
            f"SKILL.md missing required 'name' field: {source_path}"
        )

    version = str(fm.get("version", "1.0.0"))
    description = (fm.get("description") or "")
    if isinstance(description, str):
        description = description.strip()
    else:
        description = str(description)

    # Accept both dashed (current) and underscored (forward-compat) spellings.
    raw_tools = fm.get("allowed-tools")
    if raw_tools is None:
        raw_tools = fm.get("allowed_tools")
    raw_tools = raw_tools or []
    if not isinstance(raw_tools, list):
        raise InvalidSkillFile(
            f"'allowed-tools' must be a list in {source_path}"
        )
    raw_tools_strs = [str(t) for t in raw_tools]
    allowed_tools = [_namespace_tool(t) for t in raw_tools_strs]

    sub_skills_raw = fm.get("skills") or []
    if not isinstance(sub_skills_raw, list):
        raise InvalidSkillFile(f"'skills' must be a list in {source_path}")
    sub_skills = [str(s) for s in sub_skills_raw]

    triggers_raw = fm.get("triggers") or []
    if not isinstance(triggers_raw, list):
        raise InvalidSkillFile(f"'triggers' must be a list in {source_path}")
    triggers = [str(t) for t in triggers_raw]

    return LoadedSkill(
        name=name,
        version=version,
        description=description,
        allowed_tools=allowed_tools,
        allowed_tools_raw=raw_tools_strs,
        sub_skills=sub_skills,
        triggers=triggers,
        system_prompt=body.strip(),
        source_path=source_path,
    )


def load_skill_file(path: Path) -> LoadedSkill:
    """Load a single SKILL.md by path."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise InvalidSkillFile(f"SKILL.md not found: {path}") from exc
    return _parse_skill_text(text, path)


def load_skill(skill_root: Path, name: str) -> LoadedSkill:
    """Walk ``skill_root`` recursively and return the SKILL.md whose frontmatter ``name`` matches.

    Raises:
        SkillNotFound: if no SKILL.md has that name.
        DuplicateSkill: if more than one match.
        InvalidSkillFile: if any candidate is malformed (only raised for the
            eventual match; malformed non-matches are silently skipped so one
            broken skill doesn't block lookups of other skills).
    """
    skill_root = Path(skill_root)
    matches: list[LoadedSkill] = []
    for skill_md in sorted(skill_root.rglob("SKILL.md")):
        try:
            loaded = load_skill_file(skill_md)
        except InvalidSkillFile:
            # Peek at frontmatter cheaply to decide if this file was meant to
            # be the target. If we can't tell, skip it.
            continue
        if loaded.name == name:
            matches.append(loaded)

    if not matches:
        raise SkillNotFound(name, skill_root)
    if len(matches) > 1:
        raise DuplicateSkill(name, [m.source_path for m in matches])
    return matches[0]


# ---------------------------------------------------------------------------
# Template substitution
# ---------------------------------------------------------------------------

# Placeholder = ``{name}`` where name is a Python-identifier-ish token. This
# deliberately does NOT match ``{role: "..."}`` (contains a colon/space) or
# ``{task-id}`` (contains a hyphen), so SKILL.md bodies that use brace-heavy
# JSON-like syntax survive unchanged — consistent with the spec's "literal
# string replace on ``{key}``" wording.
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render_system_prompt(skill: LoadedSkill, **substitutions: str) -> str:
    """Substitute ``{key}`` placeholders in the skill body.

    Unknown placeholders are left literal (no error, no warning here — the
    sdk_agent layer can log them if it wishes).
    """

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in substitutions:
            return str(substitutions[key])
        return match.group(0)

    return _PLACEHOLDER_RE.sub(_sub, skill.system_prompt)


# ---------------------------------------------------------------------------
# Per-team workspace provisioning
# ---------------------------------------------------------------------------


def provision_skills(
    workspace_path: Path,
    skill_root: Path | None = None,
) -> list[Path]:
    """Copy every bundled SKILL.md into ``<workspace_path>/.claude/skills/<name>/SKILL.md``.

    The workspace copies are canonical/raw — no ``{role}``/``{team_name}`` substitution
    is applied on disk. Substitution lives only in ``build_system_prompt``.

    Behaviour:
    - Discovers all SKILL.md files under ``skill_root`` (recursive).
    - For each, the destination subdirectory is the skill's ``name`` from frontmatter
      (NOT the directory name).
    - Atomic write: writes to a tempfile in the same destination directory, then
      ``os.replace`` to the final path.
    - Idempotent: if destination already exists with byte-identical content, skips the
      write entirely.
    - Returns the list of destination paths actually written. Skipped (already-identical)
      destinations are NOT returned.
    - If ``skill_root`` does not exist or contains no SKILL.md files, returns ``[]``.
    - If a SKILL.md fails to parse during discovery, skips it silently with a warning log.
    """
    if skill_root is None:
        skill_root = _bundled_skills_dir()

    skill_root = Path(skill_root)
    workspace_path = Path(workspace_path)

    if not skill_root.is_dir():
        return []

    written: list[Path] = []

    for skill_md in sorted(skill_root.rglob("SKILL.md")):
        # Parse to get the canonical skill name from frontmatter.
        try:
            text = skill_md.read_bytes()
            loaded = _parse_skill_text(text.decode("utf-8"), skill_md)
        except Exception as exc:  # InvalidSkillFile, yaml errors, decode errors
            _log.warning("provision_skills: skipping %s — %s", skill_md, exc)
            continue

        skill_name = loaded.name
        dst = workspace_path / ".claude" / "skills" / skill_name / "SKILL.md"

        # Check idempotency: skip if content is byte-identical.
        if dst.exists():
            existing = dst.read_bytes()
            if existing == text:
                continue  # already up-to-date; don't include in returned list

        # Ensure destination directory exists.
        dst.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write: tempfile in same directory, then os.replace.
        tmp_fd, tmp_name = tempfile.mkstemp(dir=dst.parent)
        try:
            os.write(tmp_fd, text)
            os.close(tmp_fd)
            os.replace(tmp_name, dst)
        except Exception:
            # Clean up temp file on failure; re-raise.
            try:
                os.close(tmp_fd)
            except OSError:
                pass
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

        written.append(dst)

    return written


# ---------------------------------------------------------------------------
# Per-agent-spawn system prompt assembly
# ---------------------------------------------------------------------------

# Verbatim section text for the persistent-agent contract block (no substitutions).
_CONTRACT_BLOCK = """\
[PERSISTENT-AGENT CONTRACT]
You do not self-exit. Completion is a state, not an exit: when your task is done,
call mcp__beidou__report_status(state="done", detail=…).

CRITICAL — completion handoff:
Before calling report_status(state="done"), you MUST emit a final assistant
message that summarizes what you accomplished. Beidou forwards exactly that
text to your leader as the completion report — there is no second chance to
add information. An empty or terse final message means an empty handoff.

Use send_message only for mid-task progress updates; it is NOT the completion
mechanism.

Inter-agent communication uses Beidou primitives only — never shell, files,
or environment variables to talk to other agents."""

_COMPLETION_HANDOFF_BLOCK = """\
[COMPLETION HANDOFF CONTRACT]
When your task is complete:
1. Emit a final assistant message summarizing what you accomplished.
   List files created, decisions made, and next-step pointers your
   leader needs.
2. Then call report_status(state="done", detail="brief summary").

If you call report_status(state="done") without providing a summary,
Beidou will ask you to provide one before your completion is forwarded
to your leader."""

_OTHER_SKILLS_BLOCK = """\
[OTHER SKILLS]
Other available skills are listed via the `Skill` tool. You MAY invoke them
when they would genuinely improve the work, but the [ASSIGNED SKILL] above is
authoritative for your role and approach."""


def build_system_prompt(skill: LoadedSkill, spawn_ctx: dict) -> str:
    """Assemble the five-section system prompt with the skill body first.

    Putting the skill body first is required for cross-agent prompt cache reuse:
    two agents using the same skill share a common cache prefix (the multi-KB
    skill block), while per-agent identity (IDENTITY block) comes after and does
    not break the prefix.

    ``spawn_ctx`` keys:
        role, role_description, team_name, workspace_path, project_workspace_path, leader_id
        (all strings; leader_id may be the user-sentinel for a root agent).

    Section order (locked):
        1. [ASSIGNED SKILL] — skill body with {role}/{role_description}/{team_name}/{workspace_path}/{project_workspace_path} substituted
        2. [IDENTITY] — per-agent identity filled from spawn_ctx
        3. [PERSISTENT-AGENT CONTRACT] — verbatim, no substitution
        4. [COMPLETION HANDOFF CONTRACT] — verbatim, no substitution
        5. [OTHER SKILLS] — verbatim, no substitution
    """
    # -- Section 1: skill body with substitutions (reuse existing helper) --
    skill_body = render_system_prompt(
        skill,
        role=spawn_ctx.get("role", ""),
        role_description=spawn_ctx.get("role_description", ""),
        team_name=spawn_ctx.get("team_name", ""),
        workspace_path=spawn_ctx.get("workspace_path", ""),
        project_workspace_path=spawn_ctx.get("project_workspace_path", ""),
    )

    skill_section = (
        "[ASSIGNED SKILL — authoritative instructions for your role]\n"
        f"──── BEGIN SKILL: {skill.name} v{skill.version} ────\n"
        f"{skill_body}\n"
        "──── END SKILL ────"
    )

    # -- Section 2: identity block --
    role = spawn_ctx.get("role", "")
    team_name = spawn_ctx.get("team_name", "")
    workspace_path = spawn_ctx.get("workspace_path", "")
    project_workspace_path = spawn_ctx.get("project_workspace_path", "")
    leader_id = spawn_ctx.get("leader_id", "unset")
    if not leader_id:
        leader_id = "unset"

    identity_section = (
        "[IDENTITY]\n"
        f"You are {role} in team {team_name}.\n"
        f"Workspace: {workspace_path}.\n"
        f"Project workspace: {project_workspace_path}.\n"
        f"Leader: {leader_id}."
    )

    # -- Sections 3, 4, and 5: verbatim blocks --
    return "\n\n".join(
        [
            skill_section,
            identity_section,
            _CONTRACT_BLOCK,
            _COMPLETION_HANDOFF_BLOCK,
            _OTHER_SKILLS_BLOCK,
        ]
    )


# ---------------------------------------------------------------------------
# SDK builtin allowlist extraction
# ---------------------------------------------------------------------------


def sdk_builtins_allowlist(allowed_tools: list[str]) -> list[str]:
    """Return the subset of ``allowed_tools`` that maps to SDK builtin tool names.

    Given the raw ``allowed-tools`` list from a SKILL.md frontmatter (which may
    contain either Beidou primitive names, legacy SDK-builtin names, or already-
    mapped names), returns only the entries that correspond to SDK built-in tools:
    ``Bash``, ``Read``, ``Write``, ``WebFetch``, ``WebSearch``.

    Beidou MCP primitives (e.g. ``create_team``, ``send_message``) and any
    unrecognised entries are filtered out. Order is preserved; duplicates are
    deduplicated while preserving first occurrence.

    Why this is needed: ``skills="all"`` in ClaudeAgentOptions auto-adds the
    ``Skill`` tool but does NOT auto-add ``Bash``/``Read``/``Write``/``WebFetch``.
    Those must come from the skill's ``allowed-tools`` via this mapping.
    """
    seen: set[str] = set()
    result: list[str] = []
    for entry in allowed_tools:
        # The entry may be a raw name (e.g. "bash") or already-mapped (e.g. "Bash").
        # Check raw name first via _SDK_BUILTIN_MAP, then check if it's already
        # one of the SDK builtin values.
        sdk_name = _SDK_BUILTIN_MAP.get(entry)
        if sdk_name is None:
            # Check if the entry is already a mapped SDK builtin value.
            if entry in _SDK_BUILTIN_MAP.values():
                sdk_name = entry
        if sdk_name is not None and sdk_name not in seen:
            seen.add(sdk_name)
            result.append(sdk_name)
    return result
