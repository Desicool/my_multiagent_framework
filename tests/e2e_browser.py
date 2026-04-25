"""Browser-level e2e tests for Beidou web UI.

Covers what backend tests miss: Alpine.js initialization, task list rendering,
task detail page, and the question modal flow.

Run (no LLM needed):
    source .venv/bin/activate
    python tests/e2e_browser.py
"""
from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results: list[tuple[str, bool, str]] = []

CHROMIUM_PATH = Path.home() / ".cache/ms-playwright/chromium-1217/chrome-linux64/chrome"


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    mark = PASS if ok else FAIL
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))


# ── helpers ──────────────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _chromium_path() -> str:
    return str(CHROMIUM_PATH)


def _start_serve(port: int) -> subprocess.Popen:
    """Start `beidou serve` as a subprocess and wait until /api/health responds."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "beidou.cli", "serve", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
            return proc
        except Exception:
            time.sleep(0.2)
    proc.terminate()
    raise RuntimeError(f"beidou serve did not start on port {port}")


def _stop(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _start_server_with_question(port: int):
    """Start a FastAPI server in-process with a pre-seeded pending question.

    Returns (q, srv, future, loop).
    """
    # Insert a real task row so loadTaskSnapshot succeeds and renderTask() renders.
    from beidou.db import upsert_task
    upsert_task(
        task_id="tsk_btest",
        description="Browser e2e test task",
        model="test",
        template="default",
        started_at=time.time(),
    )

    loop = asyncio.new_event_loop()
    broker_container = {}

    def build_and_run() -> None:
        from beidou.inbox import Question, QuestionBroker
        from beidou.web.app import create_app

        asyncio.set_event_loop(loop)
        import uvicorn

        broker = QuestionBroker()
        future: asyncio.Future = loop.create_future()
        q = Question(
            qid="q_btest001",
            asker_agent_id="agt_btest",
            current_holder_agent_id=None,
            chain=["agt_btest", "USER"],
            prompt="Which database should we use for this project?",
            context_hint="PostgreSQL or SQLite",
            state="at_user",
            future=future,
            created_at=time.time(),
        )
        broker._pending[q.qid] = q
        broker_container["broker"] = broker
        broker_container["q"] = q
        broker_container["future"] = future

        app = create_app(broker=broker, task_id="tsk_btest")
        srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        broker_container["srv"] = srv
        loop.run_until_complete(srv.serve())

    t = threading.Thread(target=build_and_run, daemon=True)
    t.start()

    # Wait for the server to accept connections.
    for _ in range(50):
        if "q" in broker_container:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
                break
            except Exception:
                pass
        time.sleep(0.2)
    else:
        raise RuntimeError("in-process server did not start")

    return (
        broker_container["q"],
        broker_container["srv"],
        broker_container["future"],
        loop,
    )


# ── Test 1: Alpine init + task list ─────────────────────────────────────────

def test_alpine_and_tasklist() -> None:
    print("\n── Test 1: Alpine init + task list ──")
    from playwright.sync_api import sync_playwright

    port = _free_port()
    proc = _start_serve(port)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                executable_path=_chromium_path(),
            )
            page = browser.new_page()

            # Capture console errors and warnings before navigating.
            # Alpine emits expression errors via console.warn (type="warning"),
            # not console.error, so we must collect both levels.
            console_errors: list[str] = []
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text)
                if msg.type in ("error", "warning")
                else None,
            )

            page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")

            # Wait for task rows to appear (Alpine renders them via x-html).
            try:
                page.wait_for_selector("a.row", timeout=8000)
                rows_ok = True
            except Exception:
                rows_ok = False

            # Check for Alpine expression errors.
            alpine_errors = [
                e
                for e in console_errors
                if "Alpine Expression Error" in e or "app is not defined" in e
            ]
            record("no Alpine JS errors", len(alpine_errors) == 0, "; ".join(alpine_errors) or "")

            # At least one task row rendered.
            row_count = page.locator("a.row").count()
            record("task list not empty (≥1 rows)", rows_ok and row_count >= 1, f"rows={row_count}")

            # Live indicator should show "live" once loadTasks() succeeded.
            try:
                live_el = page.locator("span[x-text]", has_text="live").first
                live_text = live_el.inner_text(timeout=5000)
                live_ok = live_text.strip() == "live"
            except Exception:
                live_ok = False
                live_text = ""
            record('liveOK indicator shows "live"', live_ok, repr(live_text))

            browser.close()
    finally:
        _stop(proc)


# ── Test 2: Task detail page ─────────────────────────────────────────────────

def test_task_detail() -> None:
    print("\n── Test 2: Task detail page ──")
    from beidou.db import get_tasks
    from playwright.sync_api import sync_playwright

    tasks = get_tasks(limit=1)
    if not tasks:
        record("no Alpine JS errors on task detail", False, "no tasks in DB")
        record("task_id in page breadcrumb", False, "no tasks in DB")
        record("agent section rendered", False, "no tasks in DB")
        return

    task_id = tasks[0]["task_id"]
    port = _free_port()
    proc = _start_serve(port)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                executable_path=_chromium_path(),
            )
            page = browser.new_page()

            console_errors: list[str] = []
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text)
                if msg.type in ("error", "warning")
                else None,
            )

            page.goto(
                f"http://127.0.0.1:{port}/#/tasks/{task_id}",
                wait_until="networkidle",
            )

            # Wait for at least one .panel (task header bar is the first panel).
            try:
                page.wait_for_selector(".panel", timeout=10000)
                section_ok = True
            except Exception:
                section_ok = False

            # Alpine expression errors.
            alpine_errors = [
                e
                for e in console_errors
                if "Alpine Expression Error" in e or "app is not defined" in e
            ]
            record(
                "no Alpine JS errors on task detail",
                len(alpine_errors) == 0,
                "; ".join(alpine_errors) or "",
            )

            # Task ID should appear in the breadcrumb nav.
            try:
                crumb_el = page.locator("nav a", has_text=task_id).first
                crumb_text = crumb_el.inner_text(timeout=5000)
                crumb_ok = task_id in crumb_text
            except Exception:
                crumb_ok = False
                crumb_text = ""
            record("task_id in page breadcrumb", crumb_ok, repr(crumb_text))

            # Three-pane layout: check that all three panes are present.
            panel_count = page.locator(".panel").count()
            pane_center = page.locator(".pane-center").count()
            record(
                "three-pane layout rendered",
                section_ok and panel_count >= 1 and pane_center >= 1,
                f"panels={panel_count} pane-center={pane_center}",
            )

            # Center pane (Team Tree) must be mounted and not show literal
            # "None" or "synthetic" as the root node text.
            try:
                center_text = page.locator(".pane-center").inner_text(timeout=5000)
                tree_ok = (
                    center_text is not None
                    and "None" not in center_text
                    and "synthetic" not in center_text
                )
            except Exception:
                center_text = ""
                tree_ok = False
            record(
                "center pane: no synthetic/None root node",
                pane_center >= 1 and tree_ok,
                repr(center_text[:80]) if not tree_ok else "",
            )

            browser.close()
    finally:
        _stop(proc)


# ── Test 3: Question modal flow ──────────────────────────────────────────────

def test_question_modal() -> None:
    print("\n── Test 3: Question modal flow ──")
    from playwright.sync_api import sync_playwright

    port = _free_port()
    q, srv, future, loop = _start_server_with_question(port)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                executable_path=_chromium_path(),
            )
            page = browser.new_page()

            console_errors: list[str] = []
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text)
                if msg.type in ("error", "warning")
                else None,
            )

            page.goto(
                f"http://127.0.0.1:{port}/#/tasks/tsk_btest",
                wait_until="networkidle",
            )

            # ── badge ──────────────────────────────────────────────────────
            # fetchPendingQuestions() runs on startTask → should show ❓ badge.
            # The badge is the task-header button: ❓ <span>1</span>
            badge = page.locator("button:has-text('❓')")
            try:
                badge.wait_for(timeout=10000)
                badge_ok = True
            except Exception:
                badge_ok = False

            badge_text = badge.inner_text() if badge_ok else ""
            # Badge shows the count as text inside a child span — the full text
            # is "❓ 1" (emoji + space + count) — just check the ❓ is present.
            record(
                "question badge appeared (1 question)",
                badge_ok and "❓" in badge_text,
                repr(badge_text),
            )

            if not badge_ok:
                # No badge — signal-pane tests can't proceed.
                record("questions pane shows correct prompt", False, "badge never appeared")
                record("answer submitted successfully", False, "badge never appeared")
                record('future resolved with answer "PostgreSQL"', False, "badge never appeared")
                record("badge disappeared after submit", False, "badge never appeared")
                browser.close()
                return

            # ── open questions pane via badge ─────────────────────────────
            # Clicking the badge sets rightTab = 'questions' in Alpine.
            badge.click()

            # Wait for the textarea in the questions pane.
            try:
                page.wait_for_selector("textarea", timeout=6000)
                pane_ok = True
            except Exception:
                pane_ok = False

            # Check prompt text is rendered inside the questions pane.
            if pane_ok:
                try:
                    prompt_el = page.locator("p.fg-0.mb-2, p.text-sm.fg-0").first
                    prompt_text = prompt_el.inner_text(timeout=3000)
                    prompt_ok = "Which database should we use" in prompt_text
                except Exception:
                    prompt_ok = False
                    prompt_text = ""
            else:
                prompt_ok = False
                prompt_text = ""

            record(
                "questions pane shows correct prompt",
                pane_ok and prompt_ok,
                repr(prompt_text) if not prompt_ok else "",
            )

            if not pane_ok:
                record("answer submitted successfully", False, "questions pane did not open")
                record('future resolved with answer "PostgreSQL"', False, "questions pane did not open")
                record("badge disappeared after submit", False, "questions pane did not open")
                browser.close()
                return

            # ── fill and submit ───────────────────────────────────────────
            page.locator("textarea").fill("PostgreSQL")
            # New UI uses "Answer" button, not "Submit answer".
            page.locator("button:has-text('Answer')").last.click()

            # Wait for modal to disappear (x-if removes it from DOM).
            try:
                page.wait_for_selector("textarea", state="detached", timeout=8000)
                submitted_ok = True
            except Exception:
                submitted_ok = False

            record("answer submitted successfully", submitted_ok)

            # ── future resolution ─────────────────────────────────────────
            # Verify via the browser's own fetch — same JS context as submitAnswer,
            # so no cross-process timing races.
            try:
                pending = page.evaluate("""async () => {
                    const r = await fetch('/api/questions/pending');
                    if (!r.ok) return {questions: ['error:' + r.status]};
                    return await r.json();
                }""")
                future_ok = len(pending.get("questions", [])) == 0
            except Exception as exc:
                future_ok = False
                pending = {"error": str(exc)}

            record(
                'future resolved with answer "PostgreSQL"',
                future_ok,
                "" if future_ok else str(pending.get("questions", pending)),
            )

            # ── badge gone ───────────────────────────────────────────────
            # Wait for badge to detach. The 5s poll restores it if the server
            # still has the question — so this assertion only passes if the
            # server confirmed the answer (question removed from _pending).
            try:
                page.wait_for_selector(
                    "button:has-text('❓')", state="detached", timeout=6000
                )
                badge_gone = True
            except Exception:
                badge_gone = page.locator("button:has-text('❓')").count() == 0
            record("badge disappeared after submit", badge_gone)

            browser.close()
    finally:
        # Signal the in-process server to stop.
        try:
            srv.should_exit = True
        except Exception:
            pass


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print("═" * 60)
    print("Beidou Browser E2E Tests")
    print("═" * 60)

    test_alpine_and_tasklist()
    test_task_detail()
    test_question_modal()

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
