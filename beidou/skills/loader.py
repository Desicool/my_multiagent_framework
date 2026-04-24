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
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from beidou.skills.base import Skill, parse_skill_md


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


# The eight Beidou MCP primitives from docs/tool-surface.md. Entries matching
# any of these are namespaced to ``mcp__beidou__<name>``; anything else is
# passed through verbatim for the sdk_agent layer to validate.
_BEIDOU_MCP_TOOLS: frozenset[str] = frozenset(
    {
        "send_message",
        "read_messages",
        "wait_for_message",
        "list_peers",
        "ask_user",
        "report_status",
        "create_team",
        "terminate_child",
    }
)

_MCP_PREFIX = "mcp__beidou__"

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
    _SDK_BUILTIN_MAP: dict[str, str] = {
        "bash": "Bash",
        "file_read": "Read",
        "file_write": "Write",
        "web_search": "WebSearch",
        "web_fetch": "WebFetch",
    }
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
    allowed_tools = [_namespace_tool(str(t)) for t in raw_tools]

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
