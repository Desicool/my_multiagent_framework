from beidou.skills.base import Skill, parse_skill_md
from beidou.skills.loader import (
    DuplicateSkill,
    InvalidSkillFile,
    LoadedSkill,
    SkillError,
    SkillNotFound,
    load_skill,
    load_skill_file,
    load_skills,
    render_system_prompt,
)

__all__ = [
    # Legacy API
    "Skill",
    "parse_skill_md",
    "load_skills",
    # New SDK-agent API
    "LoadedSkill",
    "load_skill",
    "load_skill_file",
    "render_system_prompt",
    "SkillError",
    "SkillNotFound",
    "DuplicateSkill",
    "InvalidSkillFile",
]
