"""validate_tool_call gate for product_manager_v2.

Enforces the "at least 2 ask_user rounds before report_status(done)" contract
from SKILL.md §1 Core DOs. The PM is required to run an open-discovery round
(round 1) and a binding-narrowing round (round 2) before submitting
requirements.md for committee review. Without this gate the contract is
prompt-only — the model can skip the interview and cobble together a
requirements doc from imagination, which has been the failure mode this
skill was built to fix.

Per docs/skill-modules.md, the gate is fail-closed: an exception or timeout
returns Block. The module is imported per-spawn (unique module name via
importlib + UUID suffix), so module-level state is naturally agent-scoped
and does not leak across PM instances or teams.

Spec:
- beidou/skills/coding_v2/product_manager/SKILL.md §1 Core DOs ("AT LEAST
  two ask_user rounds")
- docs/skill-modules.md (gate handler signature, fail-closed semantics)
- docs/tool-surface.md#ask_user, #report_status (the gated primitives)
"""
from __future__ import annotations

from beidou.agent.context import Block, Pass, ToolCallContext

# Per-spawn state. Because the HookRegistry imports this module under a
# unique name per agent spawn, this dict is effectively scoped to one
# agent. We still key by agent_id defensively in case future scoping
# changes consolidate imports.
_ASK_USER_COUNT: dict[str, int] = {}

REQUIRED_ROUNDS = 2

ASK_USER_TOOL = "mcp__beidou__ask_user"
REPORT_STATUS_TOOL = "mcp__beidou__report_status"


def _block_message(observed: int) -> str:
    return (
        "product_manager_v2 must run AT LEAST "
        f"{REQUIRED_ROUNDS} ask_user rounds before report_status(state='done'), "
        f"observed {observed}. Round 1 is open discovery (free-text or wide "
        "multi-select); round 2 is binding narrowing. Going straight to "
        "report_status without the interview is a §1 protocol violation. "
        "Spec: beidou/skills/coding_v2/product_manager/SKILL.md §1 Core DOs."
    )


async def require_two_ask_user_rounds(ctx: ToolCallContext) -> Pass | Block:
    """Count ask_user calls; gate report_status(done) until count >= REQUIRED_ROUNDS."""
    tool_name = ctx.tool_name
    agent_id = ctx.agent_id

    if tool_name == ASK_USER_TOOL:
        _ASK_USER_COUNT[agent_id] = _ASK_USER_COUNT.get(agent_id, 0) + 1
        return Pass()

    if tool_name == REPORT_STATUS_TOOL:
        state = (ctx.tool_input or {}).get("state")
        if state != "done":
            return Pass()
        observed = _ASK_USER_COUNT.get(agent_id, 0)
        if observed < REQUIRED_ROUNDS:
            ctx.emit(
                "pm_v2_gate.blocked_report_done",
                {
                    "observed_rounds": observed,
                    "required_rounds": REQUIRED_ROUNDS,
                },
            )
            return Block(reason=_block_message(observed))
        return Pass()

    return Pass()
