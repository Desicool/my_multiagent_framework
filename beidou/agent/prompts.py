"""System prompt assembly for per-agent-spawn.

Moved from ``beidou/skills/loader.py``. This module assembles the five-section
system prompt (skill body, identity, contract, completion handoff, other skills)
with template substitution for ``{role}``, ``{team_name}``, etc.

Template substitution:
   ``render_system_prompt`` — substitutes ``{key}`` placeholders in any text.
   ``build_system_prompt`` — assembles all five sections and returns the final prompt string.

``build_system_prompt`` is the canonical per-agent-spawn entry point.
"""

from __future__ import annotations

import re


# Placeholder = ``{name}`` where name is a Python-identifier-ish token. This
# deliberately does NOT match ``{role: "..."}`` (contains a colon/space) or
# ``{task-id}`` (contains a hyphen), so SKILL.md bodies that use brace-heavy
# JSON-like syntax survive unchanged — consistent with the spec's "literal
# string replace on ``{key}``" wording.
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render_system_prompt(skill, **substitutions: str) -> str:
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


# Verbatim section text for the persistent-agent contract block (no substitutions).
_CONTRACT_BLOCK = """\
[PERSISTENT-AGENT CONTRACT]
You do not self-exit. Completion is a state, not an exit: when your task is done,
call mcp__beidou__report_status(state="done", detail=…). The full
[REVIEW REQUIRED] envelope MUST live in detail (see [COMPLETION HANDOFF
CONTRACT] below); the runtime forwards detail verbatim and does not read your
final assistant message.

Use send_message only for mid-task progress updates; it is NOT the completion
mechanism.

Inter-agent communication uses Beidou primitives only — never shell, files,
or environment variables to talk to other agents.

[REPLY OBLIGATION]
When you receive an inquiry-type message (the sender called send_message
with expects_reply=True, you'll see it framed as an inquiry in your inbox),
the harness blocks every other tool call until you reply. Reply via
mcp__beidou__send_message(to=<sender>, content=...). Read tools, status
reporting, ask_user / answer_question / escalate_question, terminate_child,
and list_pending_reviews are exempt; everything else is denied with
"Cannot call <tool> — you have unreplied inquiries from: ...". Reply first
(an "acknowledged" or "deferred until X" counts), then continue.
Spec: docs/agent-runtime.md#reply-obligation, docs/tool-surface.md#send_message.

[FORBIDDEN TOOLS]
The following SDK shadow tools are blocked at two layers (SDK
disallowed_tools and a PreToolUse hook). Do not call them; use the
Beidou primitive instead.

- SendMessage  →  mcp__beidou__send_message(to=..., content=..., expects_reply=...)
- TodoWrite    →  mcp__beidou__declare_plan + mcp__beidou__update_plan_task
                  for task tracking; mcp__beidou__report_status(state='done')
                  for completion handoff. TodoWrite shadows both and is
                  rejected.

Raw AskUserQuestion is also blocked — use mcp__beidou__ask_user so the
leader chain can answer or escalate before the question reaches the user.
Spec: docs/tool-surface.md#send_message, #declare_plan, #report_status, #ask_user."""

_COMPLETION_HANDOFF_BLOCK = """\
[COMPLETION HANDOFF CONTRACT]
When your task is complete:
1. Emit a final assistant message summarizing what you accomplished.
   List files created, decisions made, and next-step pointers your
   leader needs.
2. Then call report_status(state="done", detail="<full envelope>").

On report_status(state='done'), your detail argument MUST contain the
complete [REVIEW REQUIRED] envelope (role, agent, Deliverables, Open
questions / risks, Leader action required). The runtime rejects calls
without it via envelope_missing — you will see the error in the next
tool result and must resubmit with detail filled in.

Example minimum envelope in detail:
  [REVIEW REQUIRED]
  role=<your skill>     agent=<your agent id>
  Deliverables: <what you produced>
  Open questions / risks: <any blockers, or "none">
  Leader action required: approve (terminate_child) OR rework (send_message)"""

_OTHER_SKILLS_BLOCK = """\
[OTHER SKILLS]
Other available skills are listed via the `Skill` tool. You MAY invoke them
when they would genuinely improve the work, but the [ASSIGNED SKILL] above is
authoritative for your role and approach."""


def build_system_prompt(skill, spawn_ctx: dict) -> str:
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


__all__ = ["build_system_prompt", "render_system_prompt"]
