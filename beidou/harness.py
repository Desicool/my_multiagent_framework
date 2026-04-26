"""Post-turn completion handoff repair.

When a model provider emits ToolUseBlock without preceding TextBlock,
the on_report_status PostToolUse hook can't find assistant text to
forward.  This module detects that case and returns a nudge message
to inject into the agent's inbox.

Multi-level fallback:
  L1: assistant_text in same turn           -> pass (hook handles it)   (free)
  L2: detail arg from report_status input   -> pass (hook handles it)   (free)
  L3: inject nudge asking for summary       -> agent gets extra turn    (cheap)
  L4: already nudged                        -> don't loop forever       (free)
"""

COMPLETION_HANDOFF_NUDGE = """\
Your previous turn could not be forwarded to your leader because it contained \
no summary text before the report_status call.

What happened: you called report_status(state="done") but the system had no \
TextBlock in that same turn to capture as a completion summary. \
Some model providers emit tool calls without preceding text, which breaks the \
handoff path.

Please:
1. Summarise what you accomplished — files created or changed, key decisions, \
and any next steps for your leader.
2. Then re-call: report_status(state="done", detail="your brief summary here")

This extra turn ensures your leader receives a proper handoff."""


def repair_completion_handoff(orch, caller_id, turn, nudged):
    # Only act when report_status(state="done") was called this turn.
    done_calls = [
        t for t in turn.get("tools", [])
        if "report_status" in t.get("name", "")
        and (t.get("input") or {}).get("state") == "done"
    ]
    if not done_calls:
        return None

    # L1: assistant text in the same turn — hook handles delivery.
    if turn.get("text", "").strip():
        return None

    # L2: detail arg in any done call — hook handles delivery.
    for call in done_calls:
        detail = (call.get("input") or {}).get("detail")
        if detail and str(detail).strip():
            return None

    # L3: not yet nudged — inject nudge.
    if caller_id not in nudged:
        nudged.add(caller_id)
        return COMPLETION_HANDOFF_NUDGE

    # L4: already nudged — don't loop.
    return None
