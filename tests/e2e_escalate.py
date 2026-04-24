"""E2E escalation tests.

Test 1 — 2-hop chain:   analyst → root → user
Test 2 — 3-hop chain:   analyst → team_lead → root → user

For the 3-hop chain the ctx.parent graph must be:
  analyst.ctx.parent  = team_lead.ctx
  team_lead.ctx.parent = root.ctx

This is achieved by having root create a team with ONE member (team_lead),
and team_lead itself calls create_team to spawn analyst as a sub-team.
The non-blocking create_team means team_lead is not blocked and can
check_questions / escalate_question while analyst is running.

Run:
    source ~/profiles/beidou_minimax.sh
    echo "PostgreSQL" | python tests/e2e_escalate.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys


PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    mark = PASS if ok else FAIL
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))


def _task_id_from_output(output: str) -> str | None:
    for line in output.splitlines():
        if "task_id:" in line:
            return line.split("task_id:")[-1].strip().split()[0]
        if "Beidou task tsk_" in line:
            for token in line.split():
                if token.startswith("tsk_"):
                    return token
    return None


def _events(task_id: str) -> list[dict]:
    path = os.path.expanduser(f"~/.beidou/events/{task_id}.jsonl")
    evs = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    evs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except FileNotFoundError:
        pass
    return evs


def _run(task: str, answer: str, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "beidou.cli", "run",
         "--max-question-wait", "90", task],
        input=f"{answer}\n",
        capture_output=True, text=True, env=os.environ, timeout=timeout,
    )


# ── Test 1: 2-hop  (analyst → root → user) ────────────────────────────────

def test_2hop():
    """
    root
      └─ analyst  (analyst.parent = root)
    ask_user → root inbox → root escalates → user
    """
    print("\n── Test 1: 2-hop chain (analyst → root → user) ──")

    task = (
        "You are the root orchestrator. "
        "Create a team called 'db_team' with ONE member: "
        "role='analyst', description='"
        "Call ask_user with question \"Which database?\" context=\"db choice\". "
        "Write the answer to db.txt. Do nothing else.'. "
        "After create_team: poll with await_team(timeout_seconds=5). "
        "Each poll turn call check_questions first — "
        "if ANY pending questions, call escalate_question for each with reason=\"human_needed\" "
        "BEFORE the next await_team. "
        "When done, read db.txt and report its content."
    )

    result = _run(task, "SQLite")
    combined = result.stdout + result.stderr
    task_id = _task_id_from_output(combined)
    evs = _events(task_id) if task_id else []
    ev_types = [e.get("event", e.get("event_type", "")) for e in evs]

    record("2-hop: task exit 0", result.returncode == 0, f"rc={result.returncode}")
    record("2-hop: question_asked",     "question_asked"     in ev_types, str(list(dict.fromkeys(ev_types))))
    record("2-hop: question_escalated", "question_escalated" in ev_types, "root→user")
    record("2-hop: question_answered",  "question_answered"  in ev_types)
    record("2-hop: answer in output",   "sqlite" in combined.lower() or "SQLite" in combined,
           combined[-300:] if "sqlite" not in combined.lower() else "")

    if result.returncode != 0:
        print("  stdout:", result.stdout[-600:])
        print("  stderr:", result.stderr[-300:])


# ── Test 2: 3-hop  (analyst → team_lead → root → user) ────────────────────

def test_3hop():
    """
    root
      └─ team_lead  (team_lead.parent = root)
           └─ analyst  (analyst.parent = team_lead)  ← nested create_team

    ask_user → team_lead inbox → team_lead escalates → root inbox
            → root escalates → user → answer → analyst directly
    """
    print("\n── Test 2: 3-hop chain (analyst → team_lead → root → user) ──")
    print("   Expected event sequence: question_asked → question_escalated ×2 → question_answered")

    # Root creates a team with a single team_lead member.
    # team_lead creates a sub-team with analyst.
    # analyst asks → team_lead inbox → team_lead escalates (no domain knowledge) → root inbox
    # root escalates → user → stdin answer → analyst directly
    task = (
        "You are the root orchestrator. "
        "RULE: You have no domain knowledge. Any question in your inbox MUST be "
        "escalated immediately with escalate_question — you are FORBIDDEN from answering. "
        "Step 1: Create a team 'outer' with ONE member: "
        "role='team_lead', "
        "description='STRICT RULE: You have zero domain knowledge. "
        "You are FORBIDDEN from answering any inbox question yourself. "
        "Any question must be escalated with escalate_question reason=no_authority. "
        "Step A: Create sub-team inner with ONE member: "
        "role=analyst description=\""
        "Call ask_user question=\\\"Which cloud provider should we use?\\\" "
        "context=\\\"infrastructure decision requiring human approval\\\". "
        "You MUST use ask_user. Write the answer to cloud.txt.\". "
        "Step B: Poll await_team(timeout_seconds=5). "
        "BEFORE each await_team call: call check_questions. "
        "If questions pending: call escalate_question for EACH one reason=no_authority. "
        "Repeat until await_team status=done. "
        "Then read cloud.txt and write content to result.txt.'. "
        "Step 2: After outer create_team: poll await_team(timeout_seconds=5). "
        "BEFORE each await_team: call check_questions. "
        "If questions pending: escalate_question for each reason=needs_human — "
        "do NOT answer. Repeat until done. "
        "Step 3: Read result.txt and report its exact content."
    )

    result = _run(task, "AWS", timeout=240)
    combined = result.stdout + result.stderr
    task_id = _task_id_from_output(combined)
    evs = _events(task_id) if task_id else []
    ev_types = [e.get("event", e.get("event_type", "")) for e in evs]

    # Count escalation events — should be ≥2 for a 3-hop chain
    escalation_count = ev_types.count("question_escalated")

    record("3-hop: task exit 0",        result.returncode == 0,   f"rc={result.returncode}")
    record("3-hop: question_asked",     "question_asked"     in ev_types, str(list(dict.fromkeys(ev_types))))
    record("3-hop: ≥2 question_escalated (team_lead→root, root→user)",
           escalation_count >= 2,
           f"escalation_count={escalation_count}, events={list(dict.fromkeys(ev_types))}")
    record("3-hop: question_answered",  "question_answered"  in ev_types)
    record("3-hop: answer in output",   "aws" in combined.lower() or "AWS" in combined,
           combined[-300:] if "aws" not in combined.lower() else "")

    # Verify the hop order: asked → escalated → escalated → answered
    asked_idx     = next((i for i, e in enumerate(ev_types) if e == "question_asked"), None)
    escalated_idxs = [i for i, e in enumerate(ev_types) if e == "question_escalated"]
    answered_idx  = next((i for i, e in enumerate(ev_types) if e == "question_answered"), None)

    if asked_idx is not None and len(escalated_idxs) >= 2 and answered_idx is not None:
        order_ok = (asked_idx < escalated_idxs[0] < escalated_idxs[1] < answered_idx)
        record("3-hop: event order correct (asked<esc<esc<answered)", order_ok,
               f"indices: asked={asked_idx} esc={escalated_idxs} answered={answered_idx}")
    else:
        record("3-hop: event order correct (asked<esc<esc<answered)", False,
               f"missing events — asked={asked_idx} esc={escalated_idxs} answered={answered_idx}")

    if result.returncode != 0 or escalation_count < 2:
        print("  stdout:", result.stdout[-800:])
        print("  stderr:", result.stderr[-300:])


# ── main ───────────────────────────────────────────────────────────────────

def main():
    print("═" * 60)
    print("Beidou E2E Escalation Tests")
    print("═" * 60)

    test_2hop()
    test_3hop()

    print("\n── Summary ──")
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    for name, ok, detail in results:
        mark = PASS if ok else FAIL
        print(f"  [{mark}] {name}" + (f"  ({detail})" if detail and not ok else ""))

    print(f"\n{passed}/{total} assertions passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
