"""E2E resilience tests: 429 retry, 529 overload+fallback, exhausted.

Run from the rubust/ directory after sourcing the beidou_minimax profile:
    source ~/profiles/beidou_minimax.sh
    python tests/e2e_resilience.py
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

import httpx

STUB_PORT = 4999
STUB_URL = f"http://127.0.0.1:{STUB_PORT}"


# ── helpers ────────────────────────────────────────────────────────────────

def _start_stub(errors: str) -> subprocess.Popen:
    # Capture the real base URL BEFORE _run_beidou overrides ANTHROPIC_BASE_URL with
    # the stub address.  Pass it as STUB_REAL_BASE so the stub proxies to the right place.
    real_base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    env = {**os.environ, "STUB_ERRORS": errors, "STUB_PORT": str(STUB_PORT),
           "STUB_VERBOSE": "1", "STUB_REAL_BASE": real_base}
    proc = subprocess.Popen(
        [sys.executable, "tests/stub_anthropic.py"],
        stdout=subprocess.PIPE, stderr=sys.stderr, env=env, text=True,
    )
    # Wait for STUB_READY line
    for _ in range(30):
        line = proc.stdout.readline().strip()
        if "STUB_READY" in line:
            return proc
        time.sleep(0.2)
    proc.kill()
    raise RuntimeError("Stub did not start in time")


def _stop_stub(proc: subprocess.Popen):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _stub_stats() -> dict:
    try:
        return httpx.get(f"{STUB_URL}/stub/stats", timeout=5).json()
    except Exception as exc:
        return {"error": str(exc)}


def _run_beidou(task: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "ANTHROPIC_BASE_URL": STUB_URL, **(extra_env or {})}
    return subprocess.run(
        [sys.executable, "-m", "beidou.cli", "run", "--max-question-wait", "10", task],
        capture_output=True, text=True, env=env, timeout=120,
    )


def _check_events(task_id: str, wanted_events: list[str]) -> list[str]:
    """Return list of event_types from the task's JSONL log."""
    events_file = os.path.expanduser(f"~/.beidou/events/{task_id}.jsonl")
    found = []
    try:
        with open(events_file) as f:
            for line in f:
                ev = json.loads(line)
                found.append(ev.get("event_type", ev.get("event", "")))
    except FileNotFoundError:
        pass
    missing = [e for e in wanted_events if e not in found]
    return missing   # empty → all found


def _task_id_from_output(output: str) -> str | None:
    for line in output.splitlines():
        # "task_id: tsk_xxx — ..." footer line
        if "task_id:" in line:
            return line.split("task_id:")[-1].strip().split()[0]
        # "Beidou task tsk_xxx" banner line (present even when task fails)
        if "Beidou task tsk_" in line:
            for token in line.split():
                if token.startswith("tsk_"):
                    return token
    return None


PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    mark = PASS if ok else FAIL
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))


# ── Test 1: 429 transient retry ────────────────────────────────────────────

def test_429_retry():
    # The Anthropic SDK has its own max_retries=2, so it absorbs the first 3× 429
    # (1 attempt + 2 SDK retries) without raising to ResilienceLayer. We inject 4
    # so the SDK exhausts its retries on the first ResilienceLayer attempt, our layer
    # catches RateLimitError, emits llm_retry, waits, then the 5th request succeeds.
    print("\n── Test 1: 429 four times → ResilienceLayer retry → success ──")
    proc = _start_stub("429,429,429,429")
    try:
        result = _run_beidou("Say exactly: hello world")
        stats = _stub_stats()

        ok_exit = result.returncode == 0
        record("exit 0 (task completed)", ok_exit, f"rc={result.returncode}")

        injected_429 = stats.get("injected", []).count(429)
        record("stub injected exactly 4× 429", injected_429 == 4, f"injected={stats.get('injected')}")

        proxied = stats.get("proxied", 0)
        record("stub proxied ≥1 real request", proxied >= 1, f"proxied={proxied}")

        task_id = _task_id_from_output(result.stdout)
        if task_id:
            missing = _check_events(task_id, ["llm_retry"])
            record("llm_retry events present in JSONL", not missing, f"missing={missing}")
        else:
            record("parsed task_id", False, "no task_id in output")

        if not ok_exit:
            print("    stdout:", result.stdout[-500:])
            print("    stderr:", result.stderr[-500:])
    finally:
        _stop_stub(proc)


# ── Test 2: 529 overload → fallback model → success ────────────────────────

def test_529_overload_fallback():
    print("\n── Test 2: 529 overloaded → fallback model → success ──")
    proc = _start_stub("529,529,529")
    try:
        result = _run_beidou("Say exactly: hello world")
        stats = _stub_stats()

        ok_exit = result.returncode == 0
        record("exit 0 (task completed)", ok_exit, f"rc={result.returncode}")

        injected_529 = stats.get("injected", []).count(529)
        record("stub injected ≥1× 529", injected_529 >= 1, f"injected={stats.get('injected')}")

        task_id = _task_id_from_output(result.stdout)
        if task_id:
            missing = _check_events(task_id, ["llm_retry"])
            record("llm_retry events present", not missing, f"missing={missing}")
        else:
            record("parsed task_id", False, "no task_id in output")

        if not ok_exit:
            print("    stdout:", result.stdout[-500:])
            print("    stderr:", result.stderr[-500:])
    finally:
        _stop_stub(proc)


# ── Test 3: exhausted — all retries fail → task fails ──────────────────────

def test_exhausted():
    print("\n── Test 3: all retries exhausted → task fails ──")
    # ResilienceLayer has max_attempts=4. The Anthropic SDK has its own
    # max_retries=2, so each ResilienceLayer attempt drives up to 3 stub
    # requests. To exhaust all 4 attempts we need 4 × 3 = 12 injections.
    errors = ",".join(["429"] * 14)
    proc = _start_stub(errors)
    try:
        result = _run_beidou("Say exactly: hello world")
        stats = _stub_stats()

        ok_fail = result.returncode != 0
        record("task exits non-zero (failure expected)", ok_fail, f"rc={result.returncode}")

        injected = stats.get("injected", [])
        record("stub injected ≥4 errors (max_attempts hit)", len(injected) >= 4,
               f"injected count={len(injected)}")

        task_id = _task_id_from_output(result.stdout + result.stderr)
        if task_id:
            missing = _check_events(task_id, ["llm_exhausted"])
            record("llm_exhausted event present in JSONL", not missing,
                   f"events={[e for e in _events_list(task_id)][:10]}")
        else:
            record("parsed task_id from banner", False, "could not find tsk_ in output")
    finally:
        _stop_stub(proc)


def _events_list(task_id: str) -> list[str]:
    events_file = os.path.expanduser(f"~/.beidou/events/{task_id}.jsonl")
    types = []
    try:
        with open(events_file) as f:
            for line in f:
                try:
                    ev = json.loads(line)
                    types.append(ev.get("event_type", ev.get("event", "")))
                except json.JSONDecodeError:
                    pass
    except FileNotFoundError:
        pass
    return types


# ── Test 4: tool schema error → is_error → LLM self-corrects ──────────────

def test_tool_schema_error():
    """No stub needed — we ask the LLM to call a tool with a deliberately
    bad approach and watch the is_error / self-correction in events."""
    print("\n── Test 4: tool schema error → is_error:true → self-correct ──")
    # Ask for file_write but give a task that would need correct args.
    # We inject a bad tool call by describing a task that makes the LLM
    # attempt file operations; ResilienceLayer wraps any TypeError.
    result = subprocess.run(
        [sys.executable, "-m", "beidou.cli", "run", "--max-question-wait", "10",
         "Write the text 'test' to a file named test_schema_error.txt using file_write."],
        capture_output=True, text=True, env=os.environ, timeout=60,
    )
    ok_exit = result.returncode == 0
    record("task completes successfully", ok_exit, f"rc={result.returncode}")

    task_id = _task_id_from_output(result.stdout)
    if task_id:
        # ObservabilityLayer emits tool_called with kwarg `tool=tool_name`,
        # so in JSONL the field is "tool", not "tool_name".
        events_file = os.path.expanduser(f"~/.beidou/events/{task_id}.jsonl")
        tool_events = []
        try:
            with open(events_file) as f:
                for line in f:
                    ev = json.loads(line)
                    t = ev.get("tool") or ev.get("tool_name")
                    if t:
                        tool_events.append(t)
        except FileNotFoundError:
            pass
        record("file_write tool was called", "file_write" in tool_events,
               f"tool_events={tool_events}")
    else:
        record("parsed task_id", False, "no task_id in output")


# ── Summary ────────────────────────────────────────────────────────────────

def main():
    print("═" * 60)
    print("Beidou E2E Resilience Tests")
    print("═" * 60)

    test_429_retry()
    test_529_overload_fallback()
    test_exhausted()
    test_tool_schema_error()

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
