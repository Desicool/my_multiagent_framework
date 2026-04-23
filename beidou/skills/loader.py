"""Skill discovery — scans directories in priority order."""
from __future__ import annotations

import sys
from pathlib import Path

from beidou.skills.base import Skill, parse_skill_md


def _bundled_skills_dir() -> Path:
    """Return the beidou/skills/ directory — works frozen (PyInstaller) and editable."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "beidou" / "skills"
    return Path(__file__).parent


def load_skills(extra_paths: list[Path] | None = None) -> dict[str, Skill]:
    """Scan skill directories in priority order; later entries override earlier ones.

    Order (lowest → highest priority):
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
        # Each sub-directory that contains a SKILL.md is a skill
        for skill_md in sorted(base.rglob("SKILL.md")):
            try:
                s = parse_skill_md(skill_md)
                skills[s.name] = s
            except Exception:
                pass  # skip malformed skills silently

    return skills


def skills_as_tools(skill_names: list[str], all_skills: dict[str, Skill]) -> list:
    """Return SkillTool instances for the named skills (import deferred to break cycle)."""
    from beidou.skills.tool import SkillTool

    tools = []
    for name in skill_names:
        if name in all_skills:
            tools.append(SkillTool(all_skills[name], all_skills))
    return tools
