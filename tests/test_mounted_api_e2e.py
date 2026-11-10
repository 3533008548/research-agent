"""Browser regression tests for the React workbench served by FastAPI.

Run locally after installing the optional browser dependency:

    pip install -r requirements-dev.txt
    python -m playwright install chromium
    python -m unittest tests.test_mounted_api_e2e

The fixture uses no user runtime directory or model API.  If ``E2E_REDIS_URL``
is set, it uses a private test Redis service and worker; otherwise it uses the
in-process fallback.  The suite verifies the browser-to-API contract introduced
by the FastAPI migration.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import urlopen

from tests.browser import launch_chromium


ROOT = Path(__file__).resolve().parents[1]

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipUnless(sync_playwright, "安装 requirements-dev.txt 后运行浏览器 E2E")
class TestMountedFastAPIClientE2E(unittest.TestCase):
    """React must create and cancel runs through the FastAPI run contract."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        # unittest does not call tearDown when setUp fails. Register cleanup
        # before starting the server so a failed health/browser setup is safe.
        self.addCleanup(self._cleanup)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.app = subprocess.Popen(
            [
                sys.executable, "tests/mounted_api_e2e_server.py",
                "--port", str(self.port), "--data-dir", self.tmp.name,
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._wait_until_ready("/api/v1/health")
        self.playwright = sync_playwright().start()
        self.browser = launch_chromium(self.playwright)
        self.page = self.browser.new_page()
        self.page.goto(f"{self.base_url}/", wait_until="domcontentloaded")
        self.expected_redis = bool(os.getenv("E2E_REDIS_URL", "").strip())

    def _cleanup(self) -> None:
        try:
            if hasattr(self, "browser"):
                self.browser.close()
        finally:
            try:
                if hasattr(self, "playwright"):
                    self.playwright.stop()
            finally:
                if hasattr(self, "app"):
                    self.app.terminate()
                    try:
                        self.app.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.app.kill()
                        self.app.wait(timeout=5)
                if hasattr(self, "tmp"):
                    self.tmp.cleanup()

    def _wait_until_ready(self, path: str) -> None:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if self.app.poll() is not None:
                self.fail("挂载 FastAPI E2E 服务启动失败")
            try:
                with urlopen(f"{self.base_url}{path}", timeout=0.5) as response:
                    if response.status == 200:
                        return
            except OSError:
                time.sleep(0.1)
        self.fail("挂载 FastAPI E2E 服务未在时限内就绪")

    def _state(self) -> dict:
        with urlopen(f"{self.base_url}/__e2e__/state", timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))

    def _send(self, text: str) -> None:
        self.page.locator("textarea").first.fill(text)
        self.page.get_by_role("button", name=re.compile("发送|submit", re.IGNORECASE)).click()

    def _new_session(self) -> None:
        self.page.get_by_role("button", name=re.compile("新建会话")).click()

    def _pane_width(self, selector: str) -> float:
        box = self.page.locator(selector).bounding_box()
        self.assertIsNotNone(box, selector)
        return box["width"]

    def _set_viewport(self, size):
        self.page.set_viewport_size(size)
        # ResizeObserver and React must commit width changes before measuring.
        self.page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")

    def _wait_for_width(self, selector: str, expected: float) -> None:
        self.page.wait_for_function(
            "({selector, expected}) => Math.abs(document.querySelector(selector).getBoundingClientRect().width - expected) <= 2",
            arg={"selector": selector, "expected": expected},
            timeout=3_000,
        )
        self.assertAlmostEqual(self._pane_width(selector), expected, delta=2)

    def _drag_sidebar(self, side: str, delta: float) -> None:
        name = "调整会话侧栏宽度" if side == "left" else "调整运行检查器宽度"
        handle = self.page.get_by_role("separator", name=name, exact=True)
        handle.wait_for(state="visible")
        box = handle.bounding_box()
        self.assertIsNotNone(box)
        start_x = box["x"] + box["width"] / 2
        start_y = box["y"] + min(box["height"] / 2, 60)
        self.page.mouse.move(start_x, start_y)
        self.page.mouse.down()
        self.page.mouse.move(start_x + delta, start_y, steps=8)
        self.page.mouse.up()
        self.assertNotEqual(self.page.evaluate("document.body.style.userSelect"), "none")
        self.assertNotEqual(self.page.evaluate("document.body.style.cursor"), "col-resize")

    def _assert_no_horizontal_page_overflow(self) -> None:
        self.assertTrue(self.page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth + 1"
        ))

    def _assert_panes_contain_content(self):
        for selector in (".conversation", ".message-list", ".run-inspector-content", ".research-workbench"):
            if not self.page.locator(selector).count():
                continue
            self.assertTrue(self.page.locator(selector).evaluate(
                "el => el.scrollWidth <= el.clientWidth + 1"
            ), f"{selector} must not scroll the entire pane horizontally")
        self._assert_no_horizontal_page_overflow()

    def test_right_sidebar_stays_beside_chat_when_leaving_a_wide_window(self):
        # Cross the old 1120px breakpoint in both directions, including a
        # browser zoom-equivalent CSS viewport and a short restored window.
        for width, height in ((1440, 900), (1121, 800), (1120, 800), (1000, 600),
                              (915, 600), (761, 600), (1280, 720), (1440, 900)):
            with self.subTest(viewport=(width, height)):
                self._set_viewport({"width": width, "height": height})
                self.page.wait_for_function("document.querySelector('.workspace').clientWidth === innerWidth")
                center = self.page.locator(".conversation").bounding_box()
                right = self.page.locator(".run-inspector").bounding_box()
                self.assertAlmostEqual(right["y"], center["y"], delta=1)
                self.assertGreaterEqual(right["x"], center["x"] + center["width"] - 1)
                self.assertLessEqual(right["y"] + right["height"], height + 1)
                self._assert_panes_contain_content()

    def test_long_markdown_scrolls_locally_without_widening_chat(self):
        formula = " + ".join(f"x_{{{i}}}" for i in range(55))
        content = ("布局边界测试\n\n" + "long_identifier_" * 70 + "\n\n行内公式：$" + formula + "$\n\n$$\n" + formula + "\n$$\n\n"
                   + "```text\n" + "very_long_code_" * 80 + "\n```\n\n"
                   + "| " + " | ".join(f"Column_{i}" for i in range(24)) + " |\n"
                   + "| " + " | ".join("---" for _ in range(24)) + " |\n"
                   + "| " + " | ".join("data" for _ in range(24)) + " |")
        self.page.route("**/api/v1/sessions/*/messages", lambda route: route.fulfill(
            json=[{"role": "assistant", "content": content}]))
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator(".message-body .katex-display").wait_for()
        for width in (1440, 1000, 761, 390, 1440):
            self._set_viewport({"width": width, "height": 800})
            self._assert_panes_contain_content()
            for selector in (".message-body pre", ".message-body table", ".message-body .katex-display"):
                self.assertTrue(self.page.locator(selector).evaluate("""el => {
                    const rect = el.getBoundingClientRect();
                    const body = el.closest('.message-body').getBoundingClientRect();
                    const needsScroll = el.scrollWidth > el.clientWidth;
                    el.scrollLeft = el.scrollWidth;
                    return rect.left >= body.left - 1 && rect.right <= body.right + 1
                        && (!needsScroll || el.scrollLeft > 0);
                }"""), selector)
            if width == 1000 and os.getenv("E2E_ARTIFACT_DIR"):
                self.page.locator(".message-body .katex-display").scroll_into_view_if_needed()
                self.page.screenshot(path=str(Path(os.environ["E2E_ARTIFACT_DIR"]) / "long-math-restored-window.png"))

    def test_long_research_questions_and_reports_stay_inside_the_conversation(self):
        question = ("请核查这个研究假设并比较适用条件和反例。" * 12
                    + "A Theory of Traffic Regulators for Deterministic Networks " * 6
                    + "unbroken_identifier_" * 35)
        formula = " + ".join(f"x_{{{i}}}" for i in range(45))
        report = "$$\n" + formula + "\n$$\n\n```text\n" + "wide_report_" * 90 + "\n```"
        projects = [{
            "project_id": f"layout-{i}", "question": question + f" / 方案 {i}",
            "origin_session_id": "layout-only", "goal_version": i + 1, "updated_at": "2026-11-10",
            "execution_state": "paused", "end_reason": "", "waiting_for": [], "criteria": [],
            "claims": [], "gaps": [], "evidence": [], "report": report, "report_version": i + 1,
            "budget": {"calls": 2, "max_calls": 40, "tokens": 500, "max_tokens": 20000},
        } for i in range(3)]
        self.page.route("**/api/v1/research-projects", lambda route: route.fulfill(json=projects))
        self.page.route("**/api/v1/research-projects/*", lambda route: route.fulfill(
            json=next(p for p in projects if route.request.url.endswith(p["project_id"]))))
        self.page.reload(wait_until="domcontentloaded")
        self.page.get_by_label("任务类型").select_option("research")
        self.page.locator(".research-workbench .katex-display").wait_for()
        self.assertEqual(self.page.locator(".run-inspector .research-workbench").count(), 0)
        trigger = self.page.get_by_label("选择研究问题", exact=True)
        options = self.page.get_by_role("group", name="研究问题列表", exact=True)
        for width, height in ((1440, 900), (1120, 800), (1000, 600), (761, 600), (390, 844), (1440, 900)):
            with self.subTest(viewport=(width, height)):
                self._set_viewport({"width": width, "height": height})
                trigger.click()
                options.wait_for(state="visible")
                options.scroll_into_view_if_needed()
                self._assert_panes_contain_content()
                self.assertTrue(options.evaluate("""el => {
                    const rect = el.getBoundingClientRect();
                    const pane = el.closest('.message-list').getBoundingClientRect();
                    return rect.left >= pane.left - 1 && rect.right <= pane.right + 1
                        && rect.top >= -1 && rect.bottom <= innerHeight + 1
                        && el.scrollWidth <= el.clientWidth + 1 && el.scrollHeight > el.clientHeight;
                }"""))
                self.assertEqual(options.get_by_role("button").last.inner_text(), projects[-1]["question"])
                if width == 1000 and os.getenv("E2E_ARTIFACT_DIR"):
                    self.page.screenshot(path=str(Path(os.environ["E2E_ARTIFACT_DIR"]) / "research-picker-restored-window.png"))
                options.get_by_role("button").last.click()
                options.wait_for(state="hidden")
                self.assertIn("方案 2", trigger.inner_text())
                self.assertTrue(trigger.evaluate("el => el === document.activeElement"))
                # Native disclosure keyboard behavior and Escape dismissal.
                trigger.press("Enter")
                options.wait_for(state="visible")
                trigger.press("Tab")
                self.page.keyboard.press("Space")
                options.wait_for(state="hidden")
                self.assertIn("方案 0", trigger.inner_text())
                trigger.press("Enter")
                options.wait_for(state="visible")
                trigger.press("Tab")
                self.page.keyboard.press("Escape")
                options.wait_for(state="hidden")
                self.assertTrue(trigger.evaluate("el => el === document.activeElement"))
                self._assert_panes_contain_content()
        self.assertEqual(self._state()["calls"], [])

    def _wait_for_run_status(self, expected: str, run_id: str | None = None) -> dict:
        deadline = time.monotonic() + 8
        latest: dict = {}
        while time.monotonic() < deadline:
            state = self._state()
            runs = [run for run in state["runs"] if run_id is None or run["run_id"] == run_id]
            if runs:
                latest = runs[-1]
                if latest["status"] == expected:
                    return latest
            time.sleep(0.1)
        self.fail(f"运行任务未进入 {expected} 状态：{latest}")

    def test_chat_uses_fastapi_run_and_sse(self) -> None:
        self._send("API 客户端测试")
        self.page.get_by_text("来自 FastAPI：API 客户端测试", exact=True).wait_for(timeout=8_000)

        state = self._state()
        self.assertEqual(state["uses_redis"], self.expected_redis)
        self.assertEqual(len(state["calls"]), 1)
        self.assertEqual(state["calls"][0]["message"], "API 客户端测试")
        run = self._wait_for_run_status("completed")
        self.assertEqual(run["answer"], "来自 FastAPI：API 客户端测试")

    def test_chat_layout_uses_edge_scrollbar_and_compact_composer(self):
        self._set_viewport({"width": 1700, "height": 900})
        self.page.route("**/api/v1/sessions/*/messages", lambda route: route.fulfill(json=[
            {"role": "user", "content": "解释算法四的输入和输出"},
            {"role": "assistant", "content": "算法说明。\n\n" * 70},
        ]))
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator(".message-user").wait_for()
        self.assertEqual(self.page.locator(".message-avatar, .message-role, .layout-menu").count(), 0)
        self.assertEqual(self.page.locator(".research-workbench").count(), 0)
        self.assertTrue(self.page.locator(".message-user .message-body").evaluate(
            "e => getComputedStyle(e).borderTopStyle === 'solid'"))
        self.assertTrue(self.page.locator(".message-assistant .message-body").evaluate(
            "e => getComputedStyle(e).borderTopStyle === 'none'"))
        message = self.page.get_by_label("消息", exact=True)
        compact = message.bounding_box()["height"]
        self.assertLessEqual(compact, 40)
        self.assertGreater(self.page.locator(".composer").bounding_box()["width"], 860)
        for width in (1700, 1000, 761):
            self._set_viewport({"width": width, "height": 800})
            self.page.wait_for_function("""() => {
                const list = document.querySelector('.message-list').getBoundingClientRect();
                const right = document.querySelector('.run-inspector').getBoundingClientRect();
                return Math.abs(list.right - right.left) < 2;
            }""")
            self.assertIn("rgba(0, 0, 0, 0)", self.page.locator(".message-list").evaluate(
                "e => getComputedStyle(e).scrollbarColor"))
        message.fill("很多输入内容\n" * 40)
        self.assertGreater(message.bounding_box()["height"], compact + 40)
        self.assertLessEqual(message.bounding_box()["height"], 181)
        message.fill("短输入")
        self.assertLessEqual(message.bounding_box()["height"], 40)
        self._set_viewport({"width": 1700, "height": 900})
        if os.getenv("E2E_ARTIFACT_DIR"):
            self.page.screenshot(path=str(Path(os.environ["E2E_ARTIFACT_DIR"]) / "compact-chat.png"))
        self.page.get_by_label("任务类型").select_option("research")
        self.page.locator(".message-list .research-workbench").wait_for()
        self.assertEqual(self.page.locator(".run-inspector .research-workbench").count(), 0)
        self.page.get_by_label("任务类型").select_option("chat")
        self.assertEqual(self.page.locator(".research-workbench").count(), 0)
        self.assertEqual(self._state()["calls"], [])

    def test_recovered_answer_replaces_partial_draft_and_survives_reload(self):
        self._send("__recover__")
        self.page.get_by_text("恢复前的不完整草稿", exact=True).wait_for(timeout=8_000)
        self.page.get_by_text("自动恢复后的完整回答。", exact=True).wait_for(timeout=8_000)
        self.assertEqual(self.page.get_by_text("恢复前的不完整草稿", exact=True).count(), 0)
        run = self._wait_for_run_status("completed")
        self.assertEqual(run["answer"], "自动恢复后的完整回答。")
        self.assertEqual(len(self._state()["calls"]), 1)
        self.page.reload(wait_until="domcontentloaded")
        self.page.get_by_text("自动恢复后的完整回答。", exact=True).wait_for(timeout=8_000)
        self.assertEqual(self.page.get_by_text("恢复前的不完整草稿", exact=True).count(), 0)

    @unittest.skipUnless(os.getenv("E2E_REDIS_URL"), "persistent research needs Redis")
    def test_persistent_research_controls_release_chat_and_survive_reload(self):
        self._set_viewport({"width": 1440, "height": 1000})
        self.page.get_by_label("消息", exact=True).fill("核对方案的适用条件与反例")
        self.page.get_by_label("任务类型").select_option("research")
        self.page.get_by_role("button", name="开始研究", exact=True).click()
        self.page.get_by_text("多 Agent 研究已启动。", exact=False).wait_for(timeout=10000)
        workbench = self.page.get_by_role("region", name="持续研究工作台")
        workbench.get_by_role("button", name="暂停研究", exact=True).wait_for(timeout=10000)
        self.page.get_by_label("任务类型").select_option("chat")
        self.page.get_by_label("消息", exact=True).fill("研究继续时我想问另一个问题")
        self.page.get_by_role("button", name="发送", exact=True).click()
        self.page.get_by_text("来自 FastAPI：研究继续时我想问另一个问题", exact=True).wait_for()
        self.page.get_by_label("任务类型").select_option("research")
        workbench.get_by_role("button", name="暂停研究", exact=True).click()
        workbench.get_by_text("已暂停", exact=True).wait_for()
        workbench.get_by_label("补充约束或材料标识").fill("只比较同样负载")
        workbench.get_by_role("button", name="更新研究目标", exact=True).click()
        self.page.wait_for_timeout(300)
        workbench.get_by_text("已暂停", exact=True).wait_for()
        self.page.reload(wait_until="domcontentloaded")
        self.page.get_by_label("任务类型").select_option("research")
        workbench.get_by_text("已暂停", exact=True).wait_for(timeout=10000)
        workbench.get_by_role("button", name="恢复研究", exact=True).click()
        workbench.get_by_text("持续推进中", exact=True).wait_for()
        self._assert_no_horizontal_page_overflow()
        artifact_dir = os.getenv("E2E_ARTIFACT_DIR")
        if artifact_dir:
            self.page.screenshot(path=str(Path(artifact_dir) / "persistent-research-desktop.png"))

    def test_switching_sessions_keeps_runs_and_drafts_isolated(self) -> None:
        cancelled = []
        self.page.on("request", lambda request: cancelled.append(request.url)
                     if request.url.endswith("/cancel") else None)
        self._send("__slow__")
        self.page.get_by_text("旧会话片段", exact=True).wait_for(timeout=8_000)

        self._new_session()
        self.page.get_by_text("旧会话片段", exact=True).wait_for(state="detached", timeout=3_000)
        self._send("新会话请求")
        # The second request is accepted even while A is running (a single
        # Redis consumer may queue it until A completes).
        self.page.get_by_text("新会话请求", exact=True).wait_for(timeout=3_000)
        self.page.locator(".session-item").filter(has_text="浏览器 E2E 会话").click()
        self.page.get_by_text("旧会话片段", exact=True).wait_for(timeout=3_000)
        self.page.get_by_text("新会话请求", exact=True).wait_for(state="detached")
        self.page.locator(".session-item").filter(has_text="新会话").click()
        self.page.get_by_text("来自 FastAPI：新会话请求", exact=True).wait_for(timeout=8_000)
        self.page.locator("textarea").first.fill("B 的草稿")
        self.page.locator(".session-item").filter(has_text="浏览器 E2E 会话").click()
        self.page.get_by_text("慢请求完成", exact=True).wait_for(timeout=8_000)
        self.assertEqual(self.page.locator("textarea").first.input_value(), "")
        self.page.locator(".session-item").filter(has_text="新会话").click()
        self.assertEqual(self.page.locator("textarea").first.input_value(), "B 的草稿")
        self.assertEqual(self.page.get_by_text("慢请求完成", exact=True).count(), 0)
        self.assertEqual(cancelled, [])
        state = self._state()
        self.assertEqual(len(state["calls"]), 2)
        self.assertNotEqual(state["calls"][0]["session_id"], state["calls"][1]["session_id"])
        self.assertTrue(all(run["status"] == "completed" for run in state["runs"]))

    def test_switch_before_create_run_response_does_not_cancel_it(self) -> None:
        pending = []

        def delay_response(route):
            response = route.fetch()
            pending.append((route, response))

        self.page.route("**/api/v1/runs", delay_response)
        self._send("__slow__")
        deadline = time.monotonic() + 5
        while not pending and time.monotonic() < deadline:
            self.page.wait_for_timeout(20)
        self.assertTrue(pending)
        self._new_session()
        self.page.wait_for_function("document.querySelectorAll('.session-item').length === 2")
        route, response = pending.pop()
        route.fulfill(response=response)
        self.page.unroute("**/api/v1/runs", delay_response)
        self._send("B 不受延迟影响")
        self.page.get_by_text("来自 FastAPI：B 不受延迟影响", exact=True).wait_for(timeout=8_000)
        self.assertEqual(self.page.get_by_text("慢请求完成", exact=True).count(), 0)
        self.page.locator(".session-item").filter(has_text="浏览器 E2E 会话").click()
        self.page.get_by_text("慢请求完成", exact=True).wait_for(timeout=8_000)
        self.assertTrue(all(run["status"] == "completed" for run in self._state()["runs"]))

    def test_reload_reconnects_running_session_and_restores_completed_history(self) -> None:
        self._send("__slow__")
        self.page.get_by_text("旧会话片段", exact=True).wait_for(timeout=8_000)
        self.page.reload(wait_until="domcontentloaded")
        self.page.get_by_text("旧会话片段", exact=True).wait_for(timeout=3_000)
        self.page.get_by_text("慢请求完成", exact=True).wait_for(timeout=8_000)
        self.page.reload(wait_until="domcontentloaded")
        self.page.get_by_text("慢请求完成", exact=True).wait_for(timeout=3_000)
        self.assertEqual(self.page.get_by_text("慢请求完成", exact=True).count(), 1)
        self.assertEqual(len(self._state()["calls"]), 1)

    def test_explicit_stop_still_cancels_selected_session(self) -> None:
        self._send("__slow__")
        self.page.get_by_text("旧会话片段", exact=True).wait_for(timeout=8_000)
        first_run = self._state()["runs"][0]
        self._new_session()
        self.page.wait_for_function("document.querySelectorAll('.session-item').length === 2")
        self._send("__slow__")
        self.page.get_by_text("旧会话片段", exact=True).wait_for(timeout=3_000)
        self.assertEqual(len(self._state()["runs"]), 2)
        self.assertTrue(all(run["status"] == "running" for run in self._state()["runs"]))
        self.assertEqual(self.page.locator(".session-item .status-running").count(), 2)
        self.page.locator(".session-item").filter(has_text="浏览器 E2E 会话").click()
        self.page.get_by_role("button", name="暂停任务", exact=True).first.click()
        self._wait_for_run_status("cancelled", first_run["run_id"])
        self.page.locator(".session-item").filter(has_text="新会话").click()
        self.page.get_by_text("慢请求完成", exact=True).wait_for(timeout=8_000)
        other_run = next(run for run in self._state()["runs"] if run["run_id"] != first_run["run_id"])
        self.assertEqual(other_run["status"], "completed")

    def test_late_history_response_does_not_change_selected_session(self) -> None:
        self.page.locator(".session-item.active").wait_for()
        response = self.page.request.post(f"{self.base_url}/api/v1/sessions", data={"title": "B 延迟历史"})
        session_id = response.json()["thread_id"]
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_function("document.querySelectorAll('.session-item').length === 2")
        pending = []

        def delay_history(route):
            pending.append((route, route.fetch()))

        self.page.route(f"**/sessions/{session_id}/messages", delay_history)
        self.page.locator(".session-item").filter(has_text="B 延迟历史").click()
        deadline = time.monotonic() + 5
        while not pending and time.monotonic() < deadline:
            self.page.wait_for_timeout(20)
        self.assertTrue(pending)
        self.page.locator(".session-item").filter(has_text="浏览器 E2E 会话").click()
        self.page.locator("textarea").first.fill("A 保留的草稿")
        route, response = pending.pop()
        route.fulfill(response=response)
        # Wait until the rest of B's load finishes, then check it only updated B.
        self.page.wait_for_timeout(200)
        self.assertIn("浏览器 E2E 会话", self.page.locator(".session-item.active").inner_text())
        self.assertEqual(self.page.locator("textarea").first.input_value(), "A 保留的草稿")

    def test_paper_library_keeps_relation_review_without_reproduction(self) -> None:
        self.page.locator(".session-item.active").wait_for()
        self.page.route("**/api/v1/workspace/papers", lambda route: route.fulfill(json=[{
            "paper_id": "paper-library-test", "title": "已有论文", "chunks": 3,
            "indexed_at": "2026-11-10", "relation_count": 0,
        }]))
        self.assertEqual(self.page.get_by_role("button", name="复现实验", exact=True).count(), 0)
        self.page.get_by_role("button", name="论文库", exact=True).click()
        self.page.get_by_role("button", name="关系审查", exact=True).first.wait_for()
        self.assertEqual(self.page.get_by_role("button", name="复现实验", exact=True).count(), 0)
        self.page.get_by_role("button", name="关系审查", exact=True).first.click()
        self.assertIn("【论文关系审查】", self.page.locator("textarea").first.input_value())

    def test_workspace_layout_keeps_sessions_and_tools_in_the_left_rail(self) -> None:
        # Use the fixture's real session API so navigation remains usable;
        # repeated wide characters exercise the longest persisted titles.
        for index in range(12):
            response = self.page.request.post(
                f"{self.base_url}/api/v1/sessions",
                data=json.dumps({"title": f"{index:02} " + "W" * 60}),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(response.status, 201)
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_function("document.querySelectorAll('.session-item').length === 13")

        sidebar = self.page.locator(".workspace > aside.session-sidebar")
        self.assertEqual(sidebar.count(), 1, "会话与工具必须共用工作区的一个侧栏列")
        self.assertEqual(sidebar.locator(".session-list").count(), 1)
        self.assertEqual(sidebar.locator(".sidebar-utilities").count(), 1)
        self.assertEqual(self.page.locator(".workspace > *").count(), 3)
        self.assertEqual(self.page.locator(".topbar, .conversation-heading").count(), 0)

        for width, height, layout in (
            (1440, 900, "three_columns"),
            (1000, 900, "three_columns"),
            (1000, 600, "three_columns"),
            (390, 844, "stacked"),
        ):
            with self.subTest(viewport=f"{width}x{height}"):
                self._set_viewport({"width": width, "height": height})
                self.page.evaluate("window.scrollTo(0, 0)")
                boxes = self.page.evaluate("""() => {
                    const box = (selector) => {
                        const element = document.querySelector(selector);
                        const rect = element.getBoundingClientRect();
                        return {x: rect.x, y: rect.y, right: rect.right, bottom: rect.bottom,
                                width: rect.width, height: rect.height,
                                scrollWidth: element.scrollWidth, clientWidth: element.clientWidth};
                    };
                    return {
                        workspace: box('.workspace'), sidebar: box('.session-sidebar'),
                        sessions: box('.session-list'), utilities: box('.sidebar-utilities'),
                        conversation: box('.conversation'), inspector: box('.run-inspector'),
                        composer: box('.composer'), viewport: window.innerWidth,
                        documentWidth: document.documentElement.scrollWidth,
                    };
                }""")
                self.assertAlmostEqual(boxes["workspace"]["y"], 0, delta=1)
                self.assertLessEqual(boxes["documentWidth"], boxes["viewport"] + 1)
                for name in ("workspace", "sidebar", "sessions", "utilities", "conversation", "inspector", "composer"):
                    box = boxes[name]
                    self.assertGreater(box["width"], 0, name)
                    self.assertGreater(box["height"], 0, name)
                    self.assertGreaterEqual(box["x"], -1, name)
                    self.assertLessEqual(box["right"], width + 1, name)
                    self.assertLessEqual(box["scrollWidth"], box["clientWidth"] + 1, name)
                for name in ("sessions", "utilities"):
                    self.assertGreaterEqual(boxes[name]["x"], boxes["sidebar"]["x"] - 1)
                    self.assertLessEqual(boxes[name]["right"], boxes["sidebar"]["right"] + 1)
                    self.assertGreaterEqual(boxes[name]["y"], boxes["sidebar"]["y"] - 1)
                    self.assertLessEqual(boxes[name]["bottom"], boxes["sidebar"]["bottom"] + 1)

                left, center, right = (boxes[name] for name in ("sidebar", "conversation", "inspector"))
                if layout == "three_columns":
                    self.assertAlmostEqual(left["y"], center["y"], delta=1)
                    self.assertAlmostEqual(center["y"], right["y"], delta=1)
                    self.assertLessEqual(left["right"], center["x"] + 1)
                    self.assertLessEqual(center["right"], right["x"] + 1)
                    self.assertLessEqual(boxes["composer"]["bottom"], height + 1)
                else:
                    self.assertGreaterEqual(center["y"], left["bottom"] - 1)
                    self.assertGreaterEqual(right["y"], center["bottom"] - 1)
                    self.assertAlmostEqual(left["x"], center["x"], delta=1)
                    self.assertAlmostEqual(center["width"], right["width"], delta=1)

                # A long session list must scroll independently of the tools,
                # and the composer must remain an editable target after resize.
                self.assertTrue(self.page.locator(".session-list").evaluate(
                    "element => element.scrollHeight > element.clientHeight"
                ))
                self.page.locator(".session-item").last.scroll_into_view_if_needed()
                settings = sidebar.get_by_role("button", name="设置", exact=True)
                settings.scroll_into_view_if_needed()
                self.assertTrue(settings.is_visible())
                self.assertTrue(settings.evaluate("""button => {
                    const panel = button.closest('.workspace-panel').getBoundingClientRect();
                    const toggle = button.getBoundingClientRect();
                    return toggle.bottom <= panel.bottom + 1 && toggle.top >= panel.top - 1;
                }"""), "矮窗口工具面板应滚动，不能把按钮压扁并裁切文字")
                composer = self.page.locator(".composer textarea")
                composer.fill("布局回归检查，不提交任务")
                self.assertEqual(composer.input_value(), "布局回归检查，不提交任务")
        self.assertEqual(self._state()["calls"], [])

    def test_composer_mode_switch_keeps_draft_and_fits_a_short_window(self) -> None:
        self._set_viewport({"width": 1000, "height": 600})
        composer = self.page.locator(".composer")
        message = composer.get_by_role("textbox", name="消息", exact=True)
        message.fill("保留这份研究草稿\n" * 9)
        original = message.input_value()
        mode = composer.get_by_role("combobox", name="任务类型", exact=True)
        mode.select_option("research")
        scope = composer.get_by_role("combobox", name="来源范围", exact=True)
        scope.select_option("local")
        self.assertEqual(scope.input_value(), "local")
        self.assertTrue(composer.get_by_role("button", name="继续上次研究", exact=True).is_visible())
        self.assertEqual(message.input_value(), original)
        self.assertEqual(self._state()["calls"], [])
        mode.select_option("chat")
        self.assertEqual(message.input_value(), original)
        self.assertEqual(self._state()["calls"], [])
        mode.select_option("research")
        self.assertEqual(scope.input_value(), "local")
        self.assertEqual(self._state()["calls"], [])
        self._assert_no_horizontal_page_overflow()
        self.page.wait_for_function("""() => {
            const composer = document.querySelector('.composer').getBoundingClientRect();
            const messages = document.querySelector('.message-list').getBoundingClientRect();
            const inspector = document.querySelector('.run-inspector').getBoundingClientRect();
            return composer.bottom <= innerHeight + 1 && messages.height >= 40 && inspector.top === 0;
        }""")
        send = composer.get_by_role("button", name="开始研究", exact=True)
        self.assertTrue(send.is_visible())
        self.assertLessEqual(send.bounding_box()["y"] + send.bounding_box()["height"], 600)

    def test_sidebars_resize_show_hide_and_restore_saved_preferences(self) -> None:
        self._set_viewport({"width": 1440, "height": 900})
        response = self.page.request.post(
            f"{self.base_url}/api/v1/sessions", data={"title": "W" * 20},
        )
        self.assertEqual(response.status, 201)
        title = response.json()["title"]
        self.page.reload(wait_until="domcontentloaded")
        title_element = self.page.locator(".session-item strong").filter(has_text=title)
        title_element.wait_for()

        self._wait_for_width(".session-sidebar", 240)
        self._wait_for_width(".run-inspector", 310)
        self._drag_sidebar("left", -250)
        self._wait_for_width(".session-sidebar", 190)
        narrow_title = title_element.bounding_box()
        self._drag_sidebar("left", 230)
        self._wait_for_width(".session-sidebar", 420)
        wide_title = title_element.bounding_box()
        self.assertGreater(narrow_title["height"], wide_title["height"] + 5,
                           "窄栏标题应换行，拉宽后应使用新增空间")
        self.assertTrue(title_element.evaluate("element => element.scrollWidth <= element.clientWidth + 1"))
        self.assertEqual(title_element.inner_text(), title)

        self._drag_sidebar("right", -250)
        self._wait_for_width(".run-inspector", 480)
        self.assertGreaterEqual(self._pane_width(".conversation"), 480)
        self.page.get_by_role("button", name="隐藏会话侧栏", exact=True).click()
        self.page.locator(".session-sidebar").wait_for(state="hidden")
        self.assertTrue(self.page.locator(".run-inspector").is_visible())
        self.page.get_by_role("button", name="隐藏运行检查器", exact=True).click()
        self.page.locator(".run-inspector").wait_for(state="hidden")
        self.assertTrue(self.page.get_by_role("button", name="显示会话侧栏", exact=True).is_visible())
        self.assertTrue(self.page.get_by_role("button", name="显示运行检查器", exact=True).is_visible())
        self.page.get_by_role("button", name="显示会话侧栏", exact=True).click()
        self._wait_for_width(".session-sidebar", 420)
        self.assertFalse(self.page.locator(".run-inspector").is_visible())

        self.page.reload(wait_until="domcontentloaded")
        self._wait_for_width(".session-sidebar", 420)
        self.assertFalse(self.page.locator(".run-inspector").is_visible())
        self.page.get_by_role("button", name="显示运行检查器", exact=True).click()
        self._wait_for_width(".run-inspector", 480)
        self._set_viewport({"width": 1121, "height": 900})
        self.page.wait_for_function("""() => {
            const width = selector => document.querySelector(selector).getBoundingClientRect().width;
            return width('.conversation') >= 479 && width('.session-sidebar') >= 189
                && width('.run-inspector') >= 244;
        }""")
        self._assert_no_horizontal_page_overflow()
        self._set_viewport({"width": 390, "height": 844})
        self._assert_no_horizontal_page_overflow()
        self.assertFalse(self.page.get_by_role("separator", name="调整会话侧栏宽度", exact=True).is_visible())
        self._set_viewport({"width": 1440, "height": 900})
        self._wait_for_width(".session-sidebar", 420)
        self._wait_for_width(".run-inspector", 480)

        # Keyboard resizing uses the same bounds and persists just like drag.
        handle = self.page.get_by_role("separator", name="调整会话侧栏宽度", exact=True)
        handle.focus()
        handle.press("Home")
        self._wait_for_width(".session-sidebar", 190)
        handle.press("ArrowRight")
        keyboard_width = float(handle.get_attribute("aria-valuenow"))
        self.assertGreater(keyboard_width, 190)
        self.page.reload(wait_until="domcontentloaded")
        self._wait_for_width(".session-sidebar", keyboard_width)
        self.assertEqual(self.page.get_by_label("布局选项", exact=True).count(), 0)
        self.assertEqual(self.page.get_by_role("button", name="恢复默认布局", exact=True).count(), 0)
        self.assertEqual(self._state()["calls"], [])

    def test_document_uses_workbench_width_and_respects_explicit_inspector_visibility(self) -> None:
        self._set_viewport({"width": 1440, "height": 900})
        title = "浏览器档案：验证侧栏伸缩后的正文阅读与研究工具重排"
        response = self.page.request.post(
            f"{self.base_url}/__e2e__/documents",
            data={"title": title, "content": "# 布局测试\n\n这是隔离测试中的档案正文，不调用模型。\n" * 4},
        )
        self.assertEqual(response.status, 200)
        self.page.get_by_role("button", name="科研档案", exact=True).click()
        self.page.get_by_title(title, exact=True).click()
        self.page.locator(".document-page").wait_for()
        self.page.locator(".run-inspector").wait_for(state="hidden")
        self.page.get_by_role("button", name="显示运行检查器", exact=True).click()
        self.page.locator(".run-inspector").wait_for(state="visible")
        self._drag_sidebar("left", 180)
        self._wait_for_width(".session-sidebar", 420)
        self._drag_sidebar("right", -170)
        self._wait_for_width(".run-inspector", 480)
        self.assertGreaterEqual(self._pane_width(".conversation"), 480)
        self.page.wait_for_function("""() => {
            const main = document.querySelector('.document-main').getBoundingClientRect();
            const rail = document.querySelector('.document-rail').getBoundingClientRect();
            return rail.top >= main.bottom - 1 && Math.abs(rail.width - main.width) <= 2;
        }""")
        self._assert_no_horizontal_page_overflow()
        self.page.get_by_role("button", name="隐藏会话侧栏", exact=True).click()
        self.page.wait_for_function("""() => {
            const main = document.querySelector('.document-main').getBoundingClientRect();
            const rail = document.querySelector('.document-rail').getBoundingClientRect();
            return rail.left >= main.right - 1;
        }""")
        self.page.get_by_role("button", name="隐藏运行检查器", exact=True).click()
        self.page.get_by_role("tab").first.click()
        self.page.locator(".composer").wait_for()
        self.assertFalse(self.page.locator(".run-inspector").is_visible(),
                         "切回聊天不能覆盖用户明确隐藏的右栏偏好")
        self.assertEqual(self._state()["calls"], [])

    def test_hidden_inspector_leaves_failure_details_reachable(self) -> None:
        self._set_viewport({"width": 1440, "height": 900})
        self.page.get_by_role("button", name="隐藏运行检查器", exact=True).click()
        self.page.locator(".run-inspector").wait_for(state="hidden")
        self._send("__error__")
        self._wait_for_run_status("failed")
        details = self.page.get_by_role("button", name="查看运行详情", exact=True)
        details.wait_for(state="visible", timeout=5_000)
        self.assertFalse(self.page.locator(".run-inspector").is_visible(),
                         "任务失败不应擅自覆盖隐藏偏好")
        details.click()
        self.page.locator(".run-inspector").wait_for(state="visible")
        self.assertEqual(len(self._state()["calls"]), 1)

    def test_auto_hidden_inspector_finishes_an_active_drag_before_mouseup(self) -> None:
        self._set_viewport({"width": 1440, "height": 900})
        title = "运行结束时自动隐藏的档案拖动测试"
        response = self.page.request.post(
            f"{self.base_url}/__e2e__/documents",
            data={"title": title, "content": "# 拖动测试\n\n仅使用隔离档案和确定性慢任务。"},
        )
        self.assertEqual(response.status, 200)
        self.page.get_by_role("button", name="科研档案", exact=True).click()
        self.page.get_by_title(title, exact=True).click()
        self.page.locator(".document-page").wait_for()
        self.page.locator(".run-inspector").wait_for(state="hidden")

        # Do not choose a visibility preference: the document's default
        # automatic rule must show the inspector only while the run is active.
        self.page.get_by_role("tab").first.click()
        self._send("__slow__")
        self.page.get_by_text("旧会话片段", exact=True).wait_for(timeout=8_000)
        self.page.get_by_role("tab", name=title, exact=True).click()
        self.page.locator(".run-inspector").wait_for(state="visible")
        handle = self.page.get_by_role("separator", name="调整运行检查器宽度", exact=True)
        handle.wait_for(state="visible")
        box = handle.bounding_box()
        self.assertIsNotNone(box)
        original_styles = self.page.evaluate("""() => ({
            userSelect: document.body.style.userSelect, cursor: document.body.style.cursor,
        })""")
        start_x = box["x"] + box["width"] / 2
        start_y = box["y"] + min(box["height"] / 2, 60)
        self.page.mouse.move(start_x, start_y)
        self.page.mouse.down()
        try:
            self.page.mouse.move(start_x - 40, start_y, steps=4)
            self.assertEqual(self.page.evaluate("document.body.style.userSelect"), "none")
            self.assertEqual(self.page.evaluate("document.body.style.cursor"), "col-resize")
            self._wait_for_run_status("completed")
            self.page.locator(".run-inspector").wait_for(state="hidden")
            # Hiding/unmounting a captured handle can lose pointerup. Cleanup
            # must therefore happen while the mouse button is still held.
            self.page.wait_for_function("""styles =>
                document.body.style.userSelect === styles.userSelect
                && document.body.style.cursor === styles.cursor
            """, arg=original_styles, timeout=3_000)
        finally:
            self.page.mouse.up()
        self.assertEqual(len(self._state()["calls"]), 1)

    def test_tall_research_composer_and_notice_leave_messages_and_send_reachable(self) -> None:
        self._set_viewport({"width": 1000, "height": 600})
        composer = self.page.locator(".composer")
        message = composer.get_by_role("textbox", name="消息", exact=True)
        message.fill("研究草稿")
        composer.get_by_role("combobox", name="任务类型", exact=True).select_option("research")
        original_height = message.bounding_box()["height"]
        message.fill("研究草稿\n" * 12)
        self.assertGreater(message.bounding_box()["height"], original_height + 20)
        composer.locator("input[type=file]").set_input_files({
            "name": "layout-not-supported.txt", "mimeType": "text/plain",
            "buffer": b"layout fixture; no model request",
        })
        notice = self.page.locator(".notice-error")
        notice.wait_for(state="visible")
        self.assertIn("仅支持", notice.inner_text())
        self.page.wait_for_function("""() => {
            const rect = selector => document.querySelector(selector).getBoundingClientRect();
            const messages = rect('.message-list');
            const composer = rect('.composer');
            const inspector = rect('.run-inspector');
            return messages.height >= 40 && composer.bottom <= innerHeight + 1 && inspector.top === 0
                && inspector.bottom <= innerHeight + 1;
        }""", timeout=3_000)
        send = composer.get_by_role("button", name="开始研究", exact=True)
        self.assertTrue(send.is_visible())
        self.assertTrue(send.is_enabled())
        self.assertTrue(send.evaluate("""button => {
            const target = button.getBoundingClientRect();
            const form = button.closest('form').getBoundingClientRect();
            return target.top >= form.top && target.bottom <= form.bottom + 1
                && target.bottom <= innerHeight;
        }"""), "输入框拉高并叠加通知时，发送按钮仍应在可见区域")
        self._assert_no_horizontal_page_overflow()
        self.assertEqual(self._state()["calls"], [])


    def test_http_error_diagnostics_survive_detail_reload_without_private_payloads(self) -> None:
        self._set_viewport({"width": 1440, "height": 900})
        self._send("__http401__")
        run = self._wait_for_run_status("failed")
        run_id = run["run_id"]
        inspector = self.page.locator(".run-inspector")
        diagnostics = inspector.get_by_role("region", name="错误诊断", exact=True)
        diagnostics.get_by_text("HTTP 401", exact=True).wait_for(timeout=5_000)
        self.assertIn("模型 HTTP 请求失败", diagnostics.inner_text())
        self.assertIn("接收响应头", diagnostics.inner_text())
        self.assertIn("req-browser-fixture-401", diagnostics.inner_text())
        self.assertIn(run_id, diagnostics.inner_text())
        self.assertNotIn("E2E_PRIVATE_", self.page.locator("body").inner_text())

        response = self.page.request.get(f"{self.base_url}/api/v1/runs/{run_id}")
        self.assertEqual(response.status, 200)
        detail = response.json()
        self.assertEqual(detail["error_details"]["http_status"], 401)
        self.assertEqual(detail["error_details"]["attempts"], 1)
        self.assertEqual(detail["error_details"]["retries_used"], 0)
        self.assertNotIn("E2E_PRIVATE_", json.dumps(detail))

        # A page reload intentionally clears the current-run view. The
        # durable run centre re-fetches the selected run instead of relying
        # on the previous browser's ephemeral SSE state.
        self.page.reload(wait_until="domcontentloaded")
        self.page.get_by_role("button", name="历史运行", exact=True).click()
        self.page.get_by_role("button", name=f"查看 {run_id} 的阶段时间轴", exact=True).click()
        restored = self.page.locator(".run-detail").get_by_role("region", name="错误诊断", exact=True)
        restored.get_by_text("HTTP 401", exact=True).wait_for(timeout=5_000)
        self.assertIn(run_id, restored.inner_text())
        self.assertIn("req-browser-fixture-401", restored.inner_text())
        self.assertNotIn("E2E_PRIVATE_", self.page.locator("body").inner_text())
        self.assertEqual(len(self._state()["calls"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
