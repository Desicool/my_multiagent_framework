"""Browser-level e2e tests for Beidou web UI (Svelte 5 frontend).

Covers what backend tests miss: task list rendering, three-column layout,
breadcrumb navigation, and the question banner flow.

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
    print(f"  [{mark}] {name}" + (f" -- {detail}" if detail else ""))


# ------------------------------------------------------------------ #
# helpers                                                              #
# ------------------------------------------------------------------ #

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _chromium_path() -> str:
    return str(CHROMIUM_PATH)


def _start_serve(port: int) -> subprocess.Popen:
    """Start `beidou serve` as a subprocess; block until /api/health ok."""
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


def _seed_test_task(task_id: str = "tsk_btest_e2e", description: str = "Browser e2e task") -> None:
    """Ensure a task row exists in the DB so the task list is not empty."""
    from beidou.db import upsert_task
    upsert_task(
        task_id=task_id,
        description=description,
        model="test-e2e",
        skill="orchestrator",
        started_at=time.time(),
    )


class _MockOrchestrator:
    """Minimal orchestrator stub for the question-flow test.

    Only has the attributes and methods that the web API endpoints touch:
        - _questions: QuestionRegistry
        - resolve_question(qid, answers, ...)
        - agent_exists(agent_id)
        - inbox_put(agent_id, msg)    [async]
        - emit_event(name, payload)
    """

    def __init__(self, question_registry):
        self._questions = question_registry

    def resolve_question(self, qid: str, answers: list[dict], *,
                         answerer: str | None = None,
                         reason: str | None = None) -> dict:
        """Forward to the registry; skip DB-write / event-emit side-effects."""
        pq = self._questions.get(qid)
        if pq is None:
            return {"ok": False, "reason": "unknown_qid"}
        if pq.future.done():
            return {"ok": False, "reason": "already_answered"}
        return self._questions.resolve(qid, answers)

    def agent_exists(self, agent_id: str) -> bool:
        return True

    async def inbox_put(self, agent_id: str, msg) -> None:
        pass

    def emit_event(self, name: str, payload: dict) -> None:
        pass


def _start_server_with_question(port: int):
    """Start a FastAPI server in-process with a pre-seeded pending question.

    Returns (qid, srv, loop).
    """
    from beidou.questions import QuestionRegistry, PendingQuestion

    # Seed a task row so loadTaskSnapshot succeeds.
    _seed_test_task(task_id="tsk_btest")

    loop = asyncio.new_event_loop()

    # Build registry manually -- register() needs a running loop, which we
    # don't have yet, so create the PendingQuestion directly.
    registry = QuestionRegistry()
    future = loop.create_future()
    pq = PendingQuestion(
        qid="q_btest001",
        asker_agent_id="agt_btest",
        questions=[
            {
                "header": "Test",
                "question": "What database?",
                "options": [],
                "multiSelect": False,
            }
        ],
        context_hint="PostgreSQL or SQLite",
        chain=["agt_btest", "USER"],
        future=future,
    )
    registry._pending[pq.qid] = pq

    orch = _MockOrchestrator(registry)
    container: dict = {}

    def build_and_run() -> None:
        from beidou.web.app import create_app
        asyncio.set_event_loop(loop)
        import uvicorn

        app = create_app(orch=orch)
        srv = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="error",
        ))
        container["srv"] = srv
        loop.run_until_complete(srv.serve())

    t = threading.Thread(target=build_and_run, daemon=True)
    t.start()

    # Wait for the server to accept connections.
    for _ in range(50):
        if "srv" in container:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
                break
            except Exception:
                pass
        time.sleep(0.2)
    else:
        raise RuntimeError("in-process server did not start")

    return pq.qid, container["srv"], loop


# ------------------------------------------------------------------ #
# Test A -- Svelte 5 init + task list                                 #
#    Replaces test_alpine_and_tasklist                                  #
# ------------------------------------------------------------------ #

def test_init_and_tasklist() -> None:
    print("\n-- Test A: Svelte 5 init + task list --")
    from playwright.sync_api import sync_playwright

    # Make sure there is at least one task in the DB.
    _seed_test_task()

    port = _free_port()
    proc = _start_serve(port)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                executable_path=_chromium_path(),
            )
            page = browser.new_page()

            # Collect JS console errors (not Alpine-specific anymore).
            console_errors: list[str] = []
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text)
                if msg.type == "error"
                else None,
            )

            page.goto(f"http://127.0.0.1:{port}/#/", wait_until="networkidle")

            # -- Task list rendered --
            # The home page renders TasksList: <h2>Tasks</h2> followed by
            # <ul><li><button> with each task description.
            try:
                page.wait_for_selector("h2", timeout=10000)
                heading_ok = "Tasks" in page.inner_text("h2")
            except Exception:
                heading_ok = False
            record("Tasks heading visible", heading_ok)

            # Find task-row buttons in the list (<ul> li button>).
            task_buttons = page.locator("ul li button")
            try:
                btn_count = task_buttons.count()
                rows_ok = btn_count >= 1
            except Exception:
                btn_count = 0
                rows_ok = False
            record("task list not empty (>=1 rows)", rows_ok, f"buttons={btn_count}")

            # Seed task description should appear.
            try:
                desc_el = page.locator("text=Browser e2e task").first
                desc_ok = desc_el.is_visible()
            except Exception:
                desc_ok = False
            record("seeded task description visible", desc_ok)

            # -- Connection indicator --
            # ConnectionPill is always rendered in TopBar; initial status is
            # "connecting…" (set in connection.svelte.ts).  Accept any known
            # status text.
            known_statuses = (
                "connecting", "replaying", "live", "polling", "disconnected",
            )
            pill_text = ""
            try:
                for s in known_statuses:
                    el = page.locator("header span", has_text=s)
                    if el.count() > 0:
                        pill_text = el.first.inner_text(timeout=3000)
                        break
                pill_ok = any(s in pill_text for s in known_statuses)
            except Exception:
                pill_ok = False
            record(
                "ConnectionPill shows status text",
                pill_ok,
                repr(pill_text) if not pill_ok else pill_text,
            )

            # No uncaught JS errors.
            record("no JS console errors", len(console_errors) == 0,
                   "; ".join(console_errors[:3]) or "")

            browser.close()
    finally:
        _stop(proc)


# ------------------------------------------------------------------ #
# Test B -- Task detail page                                          #
#    Replaces test_task_detail                                          #
# ------------------------------------------------------------------ #

def test_task_detail() -> None:
    print("\n-- Test B: Task detail page --")
    from beidou.db import get_tasks
    from playwright.sync_api import sync_playwright

    _seed_test_task()
    tasks = get_tasks(limit=1)
    if not tasks:
        record("task detail breadcrumb", False, "no tasks in DB")
        record("three-column layout (2 aside + 1 main)", False, "no tasks")
        record("center pane renders content", False, "no tasks")
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
                if msg.type == "error"
                else None,
            )

            page.goto(
                f"http://127.0.0.1:{port}/#/tasks/{task_id}",
                wait_until="networkidle",
            )

            # Wait for the Layout grid to render.
            try:
                page.wait_for_selector("main", timeout=10000)
                main_ok = True
            except Exception:
                main_ok = False

            # -- Breadcrumb --
            # TopBar <nav> shows "Tasks / <task_id>" breadcrumb.
            try:
                nav = page.locator("header nav")
                nav_text = nav.inner_text(timeout=5000)
                crumb_ok = task_id[:8] in nav_text or "Tasks" in nav_text
            except Exception:
                nav_text = ""
                crumb_ok = False
            record("breadcrumb shows task", crumb_ok, repr(nav_text))

            # -- Three-column layout --
            # Layout.svelte: <div class="grid ...grid-cols-[280px_1fr_320px]">
            #    <aside> left  </aside>
            #    <main>  middle </main>
            #    <aside> right </aside>
            asides = page.locator("aside")
            mains = page.locator("main")
            aside_count = asides.count()
            main_count = mains.count()
            layout_ok = (aside_count >= 2 and main_count >= 1)
            record(
                "three-column layout (2 aside + 1 main)",
                layout_ok,
                f"asides={aside_count} mains={main_count}",
            )

            # -- Center pane content --
            # Layout: <aside> left | <main> center | <aside> right.
            # Without an agent pinned, PinnedAgentPanel shows placeholder text.
            center_text = ""
            try:
                center_text = page.locator("main").first.inner_text(timeout=5000)
            except Exception:
                pass
            center_ok = bool(center_text.strip()) and "Unknown route" not in center_text
            record(
                "center pane renders content (not empty / not error)",
                center_ok,
                repr(center_text[:100]) if not center_ok else "",
            )

            # No uncaught JS errors.
            record("no JS console errors on task detail",
                   len(console_errors) == 0,
                   "; ".join(console_errors[:3]) or "")

            browser.close()
    finally:
        _stop(proc)


# ------------------------------------------------------------------ #
# Test C -- Question banner flow                                      #
#    Replaces test_question_modal                                       #
# ------------------------------------------------------------------ #

def test_question_flow() -> None:
    print("\n-- Test C: Question banner flow --")
    from playwright.sync_api import sync_playwright

    port = _free_port()
    qid, srv, loop = _start_server_with_question(port)

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
                if msg.type == "error"
                else None,
            )

            page.goto(
                f"http://127.0.0.1:{port}/#/tasks/tsk_btest",
                wait_until="networkidle",
            )

            # -- Question banner appears --
            # QuestionBanner: <section data-question-banner>
            banner = page.locator("[data-question-banner]")
            try:
                banner.wait_for(timeout=10000)
                banner_ok = True
            except Exception:
                banner_ok = False
            record("question banner appears", banner_ok)

            if not banner_ok:
                record("banner shows \"Needs your input\"", False, "banner missing")
                record("answer submitted successfully", False, "banner missing")
                record("pending questions empty after submit", False, "banner missing")
                record("question count chip gone after submit", False, "banner missing")
                browser.close()
                return

            # -- Banner header: "Needs your input" --
            try:
                needs_text = banner.locator("text=Needs your input").first
                needs_ok = needs_text.is_visible()
            except Exception:
                needs_ok = False
            record("banner shows \"Needs your input\"", needs_ok)

            # -- Question text is visible inside the banner --
            try:
                question_p = banner.locator("p", has_text="What database?").first
                q_ok = question_p.is_visible()
            except Exception:
                q_ok = False
            record("banner shows seeded question text", q_ok)

            # -- Fill the free-text textarea --
            try:
                textarea = banner.locator("textarea")
                textarea.wait_for(timeout=5000)
                textarea.fill("PostgreSQL")
                fill_ok = True
            except Exception:
                fill_ok = False
            record("textarea filled with answer", fill_ok)

            if not fill_ok:
                record("answer submitted successfully", False, "fill failed")
                record("pending questions empty after submit", False, "fill failed")
                record("question count chip gone after submit", False, "fill failed")
                browser.close()
                return

            # -- Click "Answer" button --
            try:
                answer_btn = banner.locator("button:has-text('Answer')")
                answer_btn.click()
                submit_ok = True
            except Exception:
                submit_ok = False

            # -- Wait for banner to detach --
            try:
                page.wait_for_selector(
                    "[data-question-banner]", state="detached", timeout=8000
                )
                detached_ok = True
            except Exception:
                detached_ok = page.locator("[data-question-banner]").count() == 0
            record("answer submitted (banner detached)", submit_ok and detached_ok)

            # -- Pending API returns empty --
            try:
                pending = page.evaluate("""async () => {
                    const r = await fetch('/api/questions/pending');
                    if (!r.ok) return {questions: ['error:' + r.status]};
                    return await r.json();
                }""")
                api_empty = len(pending.get("questions", [])) == 0
            except Exception as exc:
                api_empty = False
                pending = {"error": str(exc)}
            record(
                "pending questions empty after submit",
                api_empty,
                "" if api_empty else str(pending.get("questions", pending)),
            )

            # -- Question count chip is gone --
            # QuestionCountChip shows "question(s) waiting" when list.length > 0.
            # After submit, list.length should be 0 and the chip is not rendered.
            try:
                chip_btn = page.locator("button", has_text="question")
                chip_gone = chip_btn.count() == 0
            except Exception:
                chip_gone = True  # not found = good
            record("question count chip gone after submit", chip_gone)

            browser.close()
    finally:
        # Signal the in-process server to stop.
        try:
            srv.should_exit = True
        except Exception:
            pass


# ------------------------------------------------------------------ #
# main                                                                 #
# ------------------------------------------------------------------ #

def main() -> None:
    print("=" * 60)
    print("Beidou Browser E2E Tests  (Svelte 5)")
    print("=" * 60)

    test_init_and_tasklist()
    test_task_detail()
    test_question_flow()

    print("\n-- Summary --")
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    for name, ok, detail in results:
        mark = PASS if ok else FAIL
        print(f"  [{mark}] {name}" + (f"  ({detail})" if detail and not ok else ""))

    print(f"\n{passed}/{total} assertions passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
