"""Post-turn completion handoff repair (safety net).

After commit 82d5290 the canonical contract is: detail MUST contain the
[REVIEW REQUIRED] envelope, and the report_status primitive rejects calls
without it (envelope_missing). The PostToolUse hook then forwards detail
verbatim. So in practice this safety-net rarely fires — envelope_missing
catches the empty-detail case at the primitive layer and the agent sees
the error in its next tool result.

This nudge remains as a backstop for the historical edge case it was
named after: a successful report_status(state='done') call (envelope_missing
did NOT trip — detail had something) where the agent also produced no
surrounding assistant text and detail itself was somehow degraded.
The text now points at the canonical contract (envelope-in-detail).

Path semantics:
  L1: assistant_text in same turn           -> pass (hook handles it)
  L2: detail arg from report_status input   -> pass (hook handles it)
  L3: neither                                -> inject nudge (rarely reached)
  L4: already nudged                         -> don't loop forever
"""

COMPLETION_HANDOFF_NUDGE = """\
The completion handoff for your last report_status(state="done") call did \
not produce a summary for your leader.

Re-call:
  mcp__beidou__report_status(state="done", detail="<full envelope>")

The detail argument MUST contain the [REVIEW REQUIRED] envelope: role, \
agent, Deliverables, Open questions / risks, Leader action required. The \
runtime forwards detail verbatim to your leader; there is no fallback that \
reads your assistant text.

Spec: docs/agent-runtime.md#completion-reporting, \
docs/tool-surface.md#report_status."""


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
