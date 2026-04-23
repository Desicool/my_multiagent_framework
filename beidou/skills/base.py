"""Skill dataclass and SKILL.md parser."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Skill:
    name: str
    version: str
    description: str
    allowed_tools: list[str]       # beidou tool names (bash, file_read, ...)
    system_prompt: str             # SKILL.md body (markdown after frontmatter)
    model: str | None = None       # None = inherit from parent context
    sub_skills: list[str] = field(default_factory=list)  # names of nested skills
    triggers: list[str] = field(default_factory=list)    # phrases for future auto-invoke


def parse_skill_md(path: Path) -> Skill:
    """Parse a SKILL.md file into a Skill dataclass.

    Format::

        ---
        name: my_skill
        version: 1.0.0
        description: |
          What it does.
        allowed-tools:
          - bash
          - file_read
        model: claude-haiku-4-5-20251001   # optional
        skills:                             # optional sub-skills
          - other_skill
        triggers:                           # optional
          - phrase to trigger
        ---

        Markdown body becomes system_prompt.
    """
    text = path.read_text()

    # Split on YAML front-matter delimiters
    match = re.match(r"^---\n(.*?)\n---\n?(.*)", text, re.DOTALL)
    if not match:
        raise ValueError(f"SKILL.md missing front-matter delimiters: {path}")

    frontmatter_raw, body = match.group(1), match.group(2)
    fm = yaml.safe_load(frontmatter_raw) or {}

    return Skill(
        name=fm.get("name", path.parent.name),
        version=str(fm.get("version", "1.0.0")),
        description=(fm.get("description") or "").strip(),
        allowed_tools=fm.get("allowed-tools") or [],
        system_prompt=body.strip(),
        model=fm.get("model"),
        sub_skills=fm.get("skills") or [],
        triggers=fm.get("triggers") or [],
    )
