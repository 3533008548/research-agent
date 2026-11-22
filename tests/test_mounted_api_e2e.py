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
        composer = self.page.locator(".composer")
        composer.get_by_label("消息", exact=True).fill(text)
        composer.get_by_role("button", name=re.compile("发送|submit", re.IGNORECASE)).click()

    def _new_session(self) -> None:
        self.page.get_by_role("button", name=re.compile("新建会话")).click()

    def test_removed_dossier_keeps_profile_papers_and_chat_usable(self):
        self.assertEqual(self.page.get_by_role("button", name="科研档案", exact=True).count(), 0)
        response = self.page.request.get(f"{self.base_url}/api/v1/workspace/research-documents")
        self.assertEqual(response.status, 404)
        self.assertIn("application/json", response.headers["content-type"])
        self.page.get_by_role("button", name="论文库", exact=True).click()
        self.page.get_by_text("没有符合条件的论文，可导入 PDF 或调整搜索。", exact=True).wait_for()
        self.page.get_by_role("button", name="设置", exact=True).click()
        self.page.get_by_role("button", name="用户画像", exact=True).click()
        profile = self.page.get_by_role("article", name="用户画像", exact=True)
        profile.get_by_text("尚未形成用户画像。", exact=True).wait_for()
        self.assertGreater(profile.bounding_box()["width"], 300)
        self._assert_no_horizontal_page_overflow()
        self.page.get_by_role("button", name="工作台", exact=True).click()
        self._send("功能移除后的对话检查")
        self.page.locator(".message-assistant").filter(has_text="来自 FastAPI：功能移除后的对话检查").wait_for()
        self.assertEqual(len(self._state()["calls"]), 1)

    def test_tool_activity_is_live_and_survives_reload(self):
        self._set_viewport({"width": 1600, "height": 1000})
        self._send("__tools__")
        inspector = self.page.locator("#run-inspector")
        running = inspector.locator(".tool-call-running")
        running.filter(has_text="query_papers").wait_for(state="visible")
        self.assertEqual(self._state()["runs"][0]["status"], "running")
        running.filter(has_text="read_pdf").wait_for(state="visible")
        self.assertEqual(inspector.locator(".tool-call-completed").count(), 1)
        inspector.locator(".tool-call-failed").wait_for(state="visible")
        self.assertEqual(self._state()["runs"][0]["status"], "running")
        self.assertEqual(inspector.locator(".run-inspector-actions .status-running").count(), 1)
        self.page.locator(".message-assistant").filter(has_text="工具状态测试完成").wait_for(state="visible")
        self.page.reload(wait_until="domcontentloaded")
        inspector.locator(".tool-call-failed").wait_for(state="visible")
        self.assertEqual(inspector.locator(".tool-call").count(), 2)
        self.assertIn("成功 1 · 失败 1", inspector.inner_text())
        self.assertIn("工具返回了失败结果", inspector.inner_text())
        self.assertNotIn("E2E_PRIVATE", inspector.inner_text())
        self._assert_no_horizontal_page_overflow()
        output = ROOT / "runtime" / "evaluations" / "run-activity"
        output.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(output / "sidebar-tools.png"))

    def test_tool_failure_cause_is_visible_live_and_after_reload(self):
        self._set_viewport({"width": 1600, "height": 1000})
        self._send('__vision_timeout__')
        failure = self.page.locator('.tool-call-failed')
        failure.wait_for()
        failure.get_by_text('查看失败原因', exact=True).click()
        self.assertIn('LLM_RESPONSE_TIMEOUT', failure.inner_text())
        self.assertIn('未取得可用证据', failure.inner_text())
        self.assertIn('31.00 秒', failure.inner_text())
        self.assertIn('可重试，先确认无重复副作用', failure.inner_text())
        self.assertNotIn('最后 HTTP 状态', failure.inner_text())
        self.page.locator('.message-assistant').filter(has_text='工具状态测试完成').wait_for()
        self.page.reload(wait_until='domcontentloaded')
        failure.get_by_text('查看失败原因', exact=True).click()
        self.assertIn('LLM_RESPONSE_TIMEOUT', failure.inner_text())
        self.assertNotIn('E2E_PRIVATE', self.page.locator('.run-inspector').inner_text())
        self.assertIn('等待模型响应超时', self.page.locator('.run-inspector').inner_text())

    def test_verify_timeout_remains_visible_after_success_and_reload(self):
        self._set_viewport({"width": 1440, "height": 1000})
        self._send("__verify_timeout__")
        self.page.get_by_text("已保留的正常回复", exact=True).wait_for()
        inspector = self.page.locator("#run-inspector")
        inspector.get_by_text("事实核验未完成，本轮回复未经完整核验。", exact=True).wait_for()
        self.page.reload(wait_until="domcontentloaded")
        inspector.get_by_text("事实核验未完成，本轮回复未经完整核验。", exact=True).wait_for()
        inspector.locator(".run-stage-history > summary").click()
        inspector.get_by_text("核验超时", exact=True).wait_for()
        inspector.get_by_text("等待模型服务", exact=True).wait_for()
        self.assertNotIn("等待材料", inspector.inner_text())
        inspector.get_by_text("查看失败原因", exact=True).click()
        inspector.get_by_text("45.00 秒", exact=True).wait_for()
        self.assertIn("LLM_RESPONSE_TIMEOUT", inspector.inner_text())
        self.assertNotIn("PRIVATE_DIAGNOSTIC", inspector.inner_text())
        self.assertEqual(inspector.locator(".run-inspector-actions .status-completed").count(), 1)

    def test_verification_rounds_and_unresolved_verdict_survive_reload(self):
        self._set_viewport({"width": 1440, "height": 1000})
        self._send("__verify_repair_limit__")
        self.page.get_by_text("一次修正后的回复，仍有问题待确认。", exact=True).wait_for()
        self.page.reload(wait_until="domcontentloaded")
        inspector = self.page.locator("#run-inspector")
        inspector.get_by_text("仍有待确认的事实问题；已达到一次自动修正上限。", exact=True).wait_for()
        inspector.locator(".run-stage-history > summary").click()
        inspector.get_by_text("第 1/2 次核验 · 定向修正中", exact=True).wait_for()
        inspector.get_by_text("第 2/2 次核验 · 仍有待确认问题", exact=True).wait_for()
        self.assertNotIn("核验通过", inspector.inner_text())
        self.assertEqual(inspector.locator(".run-inspector-actions .status-completed").count(), 1)

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
                              (915, 600), (817, 600), (1280, 720), (1440, 900)):
            with self.subTest(viewport=(width, height)):
                self._set_viewport({"width": width, "height": height})
                self.page.wait_for_function("document.querySelector('.workspace').clientWidth === innerWidth - 56")
                center = self.page.locator(".conversation").bounding_box()
                right = self.page.locator(".run-inspector").bounding_box()
                self.assertAlmostEqual(right["y"], center["y"], delta=1)
                self.assertGreaterEqual(right["x"], center["x"] + center["width"] - 1)
                self.assertLessEqual(right["y"] + right["height"], height + 1)
                self._assert_panes_contain_content()

    def test_long_markdown_scrolls_locally_without_widening_chat(self):
        formula = " + ".join(f"x_{{{i}}}" for i in range(55))
        content = ("布局边界测试\n\n" + "long_identifier_" * 70 + "\n\n行内公式：$" + formula + "$\n\n$$\n" + formula + "\\tag{1}\n$$\n\n"
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
            self.assertTrue(self.page.locator('.message-body .katex-display').evaluate("""el => {
                el.scrollLeft = 0;
                const body = el.querySelector('.katex-html');
                const parts = Array.from(body.children).filter(n => !n.classList.contains('tag')).map(n => n.getBoundingClientRect());
                return Math.min(...parts.map(r => r.left)) >= el.getBoundingClientRect().left - 1
                    && body.querySelector('.tag').getBoundingClientRect().left >= Math.max(...parts.map(r => r.right));
            }"""), "长公式的起点可见且编号不重叠")
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

    def test_chat_table_keeps_formula_pipes_inside_their_cells(self):
        content = r"""归一化方式对比：

| 方式 | 含义 | 结果 |
| --- | --- | --- |
| 除以 2 | $\alpha,\beta\in[0,1]$，两项取平均 | 合并后仍落在区间内 |
| 除以 $C_{|S|}^{2}$ | 对所有流对求平均 | 合并后仍落在区间内 |
| 绝对值 \(|x-y|\) | 范数 $\|x\|$ | 条件概率 $P(A|B)$ |

正文继续保留。"""
        self.page.route("**/api/v1/sessions/*/messages", lambda route: route.fulfill(
            json=[{"role": "assistant", "content": content}]))
        self.page.reload(wait_until="domcontentloaded")
        body = self.page.locator(".message-assistant .message-body")
        body.locator(".katex").first.wait_for()
        table = body.locator("table")
        self.assertEqual(table.locator("tbody td").count(), 9)
        self.assertEqual(table.locator(".katex").count(), 5)
        self.assertEqual(table.locator("tbody tr").nth(1).locator("td").nth(1).inner_text(), "对所有流对求平均")
        cardinality = table.locator("tbody tr").nth(1).locator("td").first
        self.assertEqual(cardinality.locator("annotation").text_content(), "C_{|S|}^{2}")
        self.assertEqual(body.locator(".math-fallback, .katex-error").count(), 0)
        self.assertNotIn("MATH_PIPE", body.inner_text())
        self.assertNotIn("$C_", body.inner_text())
        for width in (1440, 390, 1440):
            self._set_viewport({"width": width, "height": 900})
            self._assert_panes_contain_content()
        output = ROOT / "runtime" / "evaluations" / "translation-math"
        output.mkdir(parents=True, exist_ok=True)
        table.screenshot(path=str(output / "chat-table-formulas.png"))
        self.page.reload(wait_until="domcontentloaded")
        cardinality.locator(".katex").wait_for()
        self.assertEqual(cardinality.locator("annotation").text_content(), "C_{|S|}^{2}")

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
        for width in (1700, 1000, 817):
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

    def test_stream_follows_bottom_but_preserves_reading_position(self):
        self._set_viewport({"width": 1440, "height": 800})
        self._send("__scroll__")
        self.page.get_by_text("流式新增第 2 段。", exact=True).wait_for()
        list_el = self.page.locator(".message-list")
        self.page.wait_for_function("""() => {
            const e = document.querySelector('.message-list');
            return e.scrollHeight > e.clientHeight + 100 && e.scrollHeight - e.clientHeight - e.scrollTop < 3;
        }""")
        # Use real wheel input, not DOM mutation, while tokens continue arriving.
        list_el.hover()
        self.page.mouse.wheel(0, -600)
        self.page.wait_for_function("""() => {
            const e = document.querySelector('.message-list');
            return e.scrollHeight - e.clientHeight - e.scrollTop > 400;
        }""")
        self.page.wait_for_timeout(200)
        position = list_el.evaluate("e => ({top: e.scrollTop, height: e.scrollHeight})")
        self.page.wait_for_function("document.querySelector('.message-list').scrollHeight > " + str(position["height"] + 120))
        self.assertAlmostEqual(list_el.evaluate("e => e.scrollTop"), position["top"], delta=3)
        # Returning to the bottom explicitly resumes following new content.
        self.page.mouse.wheel(0, 100000)
        self.page.wait_for_timeout(250)
        height = list_el.evaluate("e => e.scrollHeight")
        self.page.wait_for_function("""height => {
            const e = document.querySelector('.message-list');
            return e.scrollHeight > height + 100 && e.scrollHeight - e.clientHeight - e.scrollTop < 3;
        }""", arg=height)
        self.page.mouse.wheel(0, -650)
        self.page.wait_for_timeout(250)
        top = list_el.evaluate("e => e.scrollTop")
        self._wait_for_run_status("completed")
        self.page.wait_for_timeout(250)
        self.assertAlmostEqual(list_el.evaluate("e => e.scrollTop"), top, delta=3)

    def test_timer_ticks_freezes_and_restores_after_reload(self):
        self._send("__slow__")
        timer = self.page.locator('.message-assistant [role="timer"]')
        timer.wait_for()
        self.assertEqual(timer.get_attribute("data-running"), "true")
        first = timer.inner_text()
        self.page.wait_for_function("text => document.querySelector('[role=timer]').textContent !== text", arg=first)
        self.page.reload(wait_until="domcontentloaded")
        timer.wait_for()
        self.assertNotIn("00:00", timer.inner_text())
        self.page.get_by_text("慢请求完成", exact=True).wait_for(timeout=8000)
        self.page.wait_for_function("document.querySelector('[role=timer]').dataset.running === 'false'")
        final = timer.inner_text()
        self.page.wait_for_timeout(1200)
        self.assertEqual(timer.inner_text(), final)
        self.page.reload(wait_until="domcontentloaded")
        timer.wait_for()
        self.assertEqual(timer.inner_text(), final)
        self.assertEqual(self.page.locator('.message-user [role="timer"]').count(), 0)
        self.assertEqual(timer.evaluate("e => e.parentElement.firstElementChild === e"), True)
        if os.getenv("E2E_ARTIFACT_DIR"):
            self.page.screenshot(path=str(Path(os.environ["E2E_ARTIFACT_DIR"]) / "run-timer.png"))

    def test_timer_stops_on_failure_and_cancellation(self):
        self._send("__error__")
        self._wait_for_run_status("failed")
        timer = self.page.locator('.message-assistant [role="timer"]').last
        self.page.wait_for_function("document.querySelector('[role=timer]').dataset.running === 'false'")
        failed = timer.inner_text()
        self._send("__slow__")
        self.page.get_by_text("旧会话片段", exact=True).wait_for()
        running_id = next(run["run_id"] for run in self._state()["runs"] if run["status"] == "running")
        self.page.get_by_role("button", name="暂停任务", exact=True).first.click()
        self._wait_for_run_status("cancelled", running_id)
        self.page.wait_for_function("Array.from(document.querySelectorAll('[role=timer]')).every(e => e.dataset.running === 'false')")
        cancelled = timer.inner_text()
        self.page.wait_for_timeout(1200)
        self.assertEqual(timer.inner_text(), cancelled)
        self.assertEqual(self.page.locator('[role="timer"]').first.inner_text(), failed)

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
    def test_parallel_research_tool_activity_polls_and_persists(self):
        self._set_viewport({"width": 1440, "height": 1000})
        self.page.get_by_label("消息", exact=True).fill("研究工具记录测试")
        self.page.get_by_label("任务类型").select_option("research")
        self.page.get_by_role("button", name="开始研究", exact=True).click()
        self.page.get_by_text("多 Agent 研究已启动。", exact=False).wait_for(timeout=10000)
        response = self.page.request.post(f"{self.base_url}/__e2e__/research-activity", data={"phase": "start"})
        self.assertEqual(response.status, 200)
        inspector = self.page.locator("#run-inspector")
        inspector.locator(".tool-call-running").filter(has_text="研究员一").wait_for(timeout=10000)
        inspector.locator(".tool-call-running").filter(has_text="研究员二").wait_for(timeout=10000)
        self.assertEqual(inspector.locator(".tool-call").count(), 2)
        self.page.request.post(f"{self.base_url}/__e2e__/research-activity", data={"phase": "finish"})
        inspector.get_by_text("执行中 0 · 成功 2 · 失败 0", exact=True).wait_for(timeout=10000)
        self.page.reload(wait_until="domcontentloaded")
        inspector.get_by_text("执行中 0 · 成功 2 · 失败 0", exact=True).wait_for(timeout=10000)
        self.assertEqual(inspector.locator(".tool-call").count(), 2)
        self._assert_no_horizontal_page_overflow()

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
        intercepted = False

        def delay_response(route):
            nonlocal intercepted
            if intercepted:
                route.continue_()
                return
            intercepted = True
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
        # Keep interception enabled while the first SSE connection is opening.
        # Disabling it here can strand in-flight Chromium requests; subsequent
        # POSTs pass through without delay so only A's response is held.
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

    def test_paper_library_keeps_reading_without_relation_review(self) -> None:
        self.page.locator(".session-item.active").wait_for()
        self.page.get_by_label("消息", exact=True).fill("我的阅读草稿")
        self.page.route("**/api/v1/workspace/papers", lambda route: route.fulfill(json=[{
            "paper_id": "paper-library-test", "title": "已有论文", "chunks": 3,
            "indexed_at": "2026-11-10", "relation_count": 0,
        }]))
        import fitz
        with fitz.open() as document:
            document.new_page().insert_text((50, 80), "An indexed library paper")
            uploaded = self.page.request.post(f"{self.base_url}/api/v1/library/documents", multipart={
                'file': {'name': '已有论文.pdf', 'mimeType': 'application/pdf', 'buffer': document.tobytes()},
            })
        self.assertEqual(uploaded.status, 201)
        self.assertEqual(self.page.get_by_role("button", name="复现实验", exact=True).count(), 0)
        self.page.get_by_role("button", name="论文库", exact=True).click()
        self.page.get_by_role("checkbox", name="选择 已有论文.pdf", exact=True).check()
        self.page.get_by_role("button", name="交给助手阅读", exact=True).wait_for()
        self.assertEqual(self.page.get_by_role("button", name="复现实验", exact=True).count(), 0)
        self.assertEqual(self.page.get_by_role("button", name="关系审查", exact=True).count(), 0)
        self.page.get_by_role("button", name="阅读并提问", exact=True).click()
        self.page.locator(".textLayer span").filter(has_text="An indexed library paper").first.wait_for()
        composer = self.page.locator(".right-workspace .composer")
        self.assertEqual(composer.get_by_label("消息", exact=True).input_value(), "我的阅读草稿")
        self.assertEqual(self._state()["calls"], [])

    def test_application_pages_preserve_conversation_reader_and_unsaved_settings(self) -> None:
        self._set_viewport({"width": 1440, "height": 900})
        self.page.locator(".session-item.active").wait_for()
        import fitz
        with fitz.open() as document:
            document.new_page().insert_text((50, 80), "Navigation fixture, first page")
            document.new_page().insert_text((50, 80), "Navigation fixture, second page")
            uploaded = self.page.request.post(f"{self.base_url}/api/v1/library/documents", multipart={
                "file": {"name": "navigation.pdf", "mimeType": "application/pdf", "buffer": document.tobytes()},
            })
        self.assertEqual(uploaded.status, 201)
        navigation = self.page.get_by_role("navigation", name="应用导航", exact=True)
        message = self.page.get_by_label("消息", exact=True)
        message.fill("切换页面后继续写的草稿")
        self.page.evaluate("window.originalComposer = document.querySelector('.composer')")
        navigation.get_by_role("button", name="论文库", exact=True).click()
        self.assertFalse(self.page.locator(".workspace").is_visible())
        self.assertEqual(self.page.get_by_role("tab").count(), 0)
        search = self.page.get_by_role("textbox", name="搜索论文", exact=True)
        search.fill("navigation")
        self.page.get_by_role("checkbox", name="选择 navigation.pdf", exact=True).check()
        self.assertTrue(self.page.get_by_role("complementary", name="文件详情", exact=True).is_visible())
        navigation.get_by_role("button", name="设置", exact=True).click()
        self.page.get_by_role("textbox", name="模型", exact=True).fill("unsaved-model-draft")
        navigation.get_by_role("button", name="论文库", exact=True).click()
        self.assertEqual(search.input_value(), "navigation")
        self.assertTrue(self.page.get_by_role("checkbox", name="选择 navigation.pdf", exact=True).is_checked())
        self.page.get_by_role("button", name="navigation.pdf", exact=True).click()
        self.page.locator(".textLayer span").first.wait_for()
        self.assertEqual(navigation.get_by_role("button", name="工作台", exact=True).get_attribute("aria-current"), "page")
        self.page.get_by_role("button", name="下一页", exact=True).click()
        self.page.locator(".textLayer span").filter(has_text="second page").wait_for()
        self.page.get_by_role("spinbutton", name="缩放比例", exact=True).fill("150")
        self.page.get_by_role("spinbutton", name="缩放比例", exact=True).blur()
        self.page.wait_for_function("document.querySelector('.pdf-viewport').scrollHeight > 1000")
        self.page.locator('.pdf-viewport').evaluate("el => el.scrollTop = 300")
        self.page.wait_for_timeout(100)
        reading_top = self.page.locator('.pdf-viewport').evaluate("el => el.scrollTop")
        self.page.evaluate("window.originalCanvas = document.querySelector('.pdf-sheet canvas')")
        navigation.get_by_role("button", name="设置", exact=True).click()
        self.assertEqual(self.page.get_by_role("textbox", name="模型", exact=True).input_value(), "unsaved-model-draft")
        navigation.get_by_role("button", name="工作台", exact=True).click()
        self.assertEqual(self.page.get_by_role("spinbutton", name="页码", exact=True).input_value(), "2")
        self.assertEqual(self.page.get_by_role("spinbutton", name="缩放比例", exact=True).input_value(), "150")
        self.page.locator('.textLayer span').first.wait_for()
        self.assertAlmostEqual(self.page.locator('.pdf-viewport').evaluate("el => el.scrollTop"), reading_top, delta=3)
        self.assertTrue(self.page.evaluate("window.originalCanvas === document.querySelector('.pdf-sheet canvas')"))
        self.assertEqual(self.page.get_by_role("tab", name="navigation.pdf", exact=True).get_attribute("aria-selected"), "true")
        self.page.locator(".conversation .draggable-chat-tab button").click()
        self.assertEqual(message.input_value(), "切换页面后继续写的草稿")
        self.assertTrue(self.page.evaluate("window.originalComposer === document.querySelector('.composer')"))
        self.assertEqual(self.page.locator('.workbench-tabs').get_by_role("tab").count(), 2)
        self.page.get_by_role("button", name="隐藏会话侧栏", exact=True).click()
        self.page.get_by_role("button", name="隐藏运行检查器", exact=True).click()
        for width in (1440, 1000, 817, 816, 390, 320, 1440):
            self._set_viewport({"width": width, "height": 900})
            for name in ("论文库", "设置", "工作台"):
                button = navigation.get_by_role("button", name=name, exact=True)
                button.focus()
                button.press("Enter")
                self.assertEqual(button.get_attribute("aria-current"), "page")
                self.assertAlmostEqual(navigation.bounding_box()["width"], 56, delta=1)
                self._assert_no_horizontal_page_overflow()
        self.assertFalse(self.page.locator('.session-sidebar').is_visible())
        self.assertFalse(self.page.locator('.run-inspector').is_visible())
        self.assertEqual(message.input_value(), "切换页面后继续写的草稿")
        self.assertEqual(self._state()["calls"], [])

    def test_application_page_switch_does_not_interrupt_a_running_chat(self) -> None:
        cancelled = []
        self.page.on("request", lambda request: cancelled.append(request.url)
                     if request.url.endswith("/cancel") else None)
        self._send("__slow__")
        self.page.get_by_text("旧会话片段", exact=True).wait_for(timeout=8_000)
        self.page.get_by_role("button", name="设置", exact=True).click()
        self.page.get_by_role("button", name="论文库", exact=True).click()
        self.page.wait_for_function("document.querySelector('.message-assistant')?.textContent.includes('慢请求完成')")
        self.assertTrue(self.page.locator('#app-page-library').is_visible(), "后台结果不应抢回工作台页面")
        self.page.get_by_role("button", name="工作台", exact=True).click()
        self.page.locator(".message-assistant").filter(has_text="慢请求完成").wait_for()
        self.assertEqual(len(self._state()["calls"]), 1)
        self.assertEqual(cancelled, [])

    def test_reader_chunk_failure_keeps_workbench_and_recovers_after_reload(self) -> None:
        import fitz
        with fitz.open() as document:
            document.new_page().insert_text((50, 80), "Reader recovery fixture")
            uploaded = self.page.request.post(f"{self.base_url}/api/v1/library/documents", multipart={
                'file': {'name': 'recovery.pdf', 'mimeType': 'application/pdf', 'buffer': document.tobytes()},
            })
        self.assertEqual(uploaded.status, 201)
        pattern = "**/assets/PaperReader-*.js"
        self.page.route(pattern, lambda route: route.abort("failed"))
        self.page.get_by_role("button", name="论文库", exact=True).click()
        self.page.get_by_role("button", name="recovery.pdf", exact=True).click()
        self.page.get_by_role("heading", name="论文预览未能打开", exact=True).wait_for()
        self.assertEqual(self.page.locator(".session-sidebar").count(), 1)
        self.page.get_by_role("button", name="设置", exact=True).click()
        self.page.get_by_role("button", name="保存通用设置", exact=True).wait_for()
        self.page.get_by_role("button", name="工作台", exact=True).click()
        self.page.unroute(pattern)
        self.page.get_by_role("button", name="重新加载界面", exact=True).click()
        self.page.get_by_role("button", name="论文库", exact=True).click()
        self.page.get_by_role("button", name="recovery.pdf", exact=True).click()
        self.page.locator(".textLayer span").first.wait_for()
        self.assertGreater(self.page.locator(".pdf-sheet canvas").evaluate("el => el.width"), 100)

    def test_workspace_layout_gives_sessions_the_full_sidebar_and_fixed_navigation(self) -> None:
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
        self.assertEqual(sidebar.count(), 1, "对话必须整体占据工作台的一列")
        self.assertEqual(sidebar.locator(".session-list").count(), 1)
        self.assertEqual(sidebar.get_by_role("button", name="设置", exact=True).count(), 0)
        self.assertEqual(sidebar.get_by_role("button", name="论文库", exact=True).count(), 0)
        self.assertEqual(self.page.locator(".app-navigation button").count(), 3)
        self.assertEqual(self.page.locator(".app-navigation [role=separator]").count(), 0)
        self.assertEqual(sidebar.get_by_role("button", name="每日检索", exact=True).count(), 0)
        self.assertEqual(sidebar.get_by_role("button", name="用户画像", exact=True).count(), 0)
        self.assertEqual(self.page.locator(".workspace > *").count(), 3)
        self.assertEqual(self.page.locator(".topbar, .conversation-heading").count(), 0)

        for width, height, layout in (
            (1440, 900, "three_columns"),
            (1000, 900, "three_columns"),
            (1000, 600, "three_columns"),
            (817, 600, "three_columns"),
            (816, 844, "stacked"),
            (390, 844, "stacked"),
            (320, 844, "stacked"),
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
                        sessions: box('.session-list'), navigation: box('.app-navigation'),
                        settings: box('.app-navigation-settings'),
                        conversation: box('.conversation'), inspector: box('.run-inspector'),
                        composer: box('.composer'), viewport: window.innerWidth,
                        documentWidth: document.documentElement.scrollWidth,
                    };
                }""")
                self.assertAlmostEqual(boxes["workspace"]["y"], 0, delta=1)
                self.assertLessEqual(boxes["documentWidth"], boxes["viewport"] + 1)
                self.assertAlmostEqual(boxes["navigation"]["width"], 56, delta=1)
                self.assertAlmostEqual(boxes["sidebar"]["x"], boxes["navigation"]["right"], delta=1)
                self.assertLessEqual(boxes["settings"]["bottom"], height + 1)
                for name in ("workspace", "sidebar", "sessions", "navigation", "conversation", "inspector", "composer"):
                    box = boxes[name]
                    self.assertGreater(box["width"], 0, name)
                    self.assertGreater(box["height"], 0, name)
                    self.assertGreaterEqual(box["x"], -1, name)
                    self.assertLessEqual(box["right"], width + 1, name)
                    self.assertLessEqual(box["scrollWidth"], box["clientWidth"] + 1, name)
                for name in ("sessions",):
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
                    self.assertGreater(boxes["sessions"]["height"], height * 0.75,
                                       "会话列表应使用侧栏高度，不再受原来的 32vh 上限限制")
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
                settings = self.page.get_by_role("button", name="设置", exact=True)
                settings.scroll_into_view_if_needed()
                self.assertTrue(settings.is_visible())
                self.assertTrue(settings.evaluate("""button => {
                    const panel = button.closest('.app-navigation').getBoundingClientRect();
                    const toggle = button.getBoundingClientRect();
                    return toggle.bottom <= panel.bottom + 1 && toggle.top >= panel.top - 1;
                }"""), "全局导航始终独立可用")
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

    def _drag_chat_tab(self, to_right: bool, cancel: bool = False) -> None:
        source = self.page.locator(".conversation .draggable-chat-tab" if to_right else ".right-workspace .draggable-chat-tab")
        box = source.bounding_box()
        self.page.mouse.move(box["x"] + 20, box["y"] + box["height"] / 2)
        self.page.mouse.down()
        self.page.mouse.move(box["x"] + 35, box["y"] + box["height"] / 2, steps=5)
        zone = self.page.locator(".chat-drop-right" if to_right else ".chat-drop-center")
        zone.wait_for(state="visible")
        if cancel:
            self.page.keyboard.press("Escape")
        else:
            target = zone.bounding_box()
            self.page.mouse.move(target["x"] + target["width"] / 2, target["y"] + 45, steps=8)
        self.page.mouse.up()
        zone.wait_for(state="hidden")
        if not cancel:
            # Dropping schedules focus on the moved tab for the next frame.
            # Wait for that before a subsequent separator keyboard operation.
            target_tab = ".right-workspace" if to_right else ".conversation"
            self.page.wait_for_function(
                "selector => document.activeElement === document.querySelector(selector)",
                arg=f"{target_tab} .draggable-chat-tab button",
            )

    def test_chat_tab_docks_beside_paper_and_returns_without_losing_state(self) -> None:
        self._set_viewport({"width": 1700, "height": 1000})
        import fitz
        with fitz.open() as document:
            document.new_page().insert_text((50, 80), "Read and discuss a paper side by side")
            uploaded = self.page.request.post(f"{self.base_url}/api/v1/library/documents", multipart={
                "file": {"name": "docking-paper.pdf", "mimeType": "application/pdf", "buffer": document.tobytes()},
            })
        self.assertEqual(uploaded.status, 201)
        self.page.get_by_label("消息", exact=True).fill("保留这份草稿")
        self.page.evaluate("window.originalComposer = document.querySelector('.composer')")
        self.page.get_by_role("button", name="论文库", exact=True).click()
        self.page.get_by_role("button", name="docking-paper.pdf", exact=True).click()
        self.page.locator(".textLayer span").first.wait_for()
        self.page.evaluate("window.originalReader = document.querySelector('.pdf-sheet canvas')")
        self._drag_chat_tab(True, cancel=True)
        self.assertEqual(self.page.locator(".conversation .draggable-chat-tab").count(), 1)
        self._drag_chat_tab(True)
        rail = self.page.locator(".right-workspace")
        rail.get_by_label("消息", exact=True).wait_for()
        self.assertEqual(rail.get_by_label("消息", exact=True).input_value(), "保留这份草稿")
        self.assertTrue(self.page.evaluate("window.originalComposer === document.querySelector('.composer')"))
        self.assertTrue(self.page.evaluate("window.originalReader === document.querySelector('.pdf-sheet canvas')"))
        self.assertTrue(self.page.locator(".paper-reader").is_visible())
        self._wait_for_width(".session-sidebar", 240)
        self._wait_for_width(".run-inspector", 480)
        for name in ("论文库", "设置", "工作台"):
            self.page.get_by_role("button", name=name, exact=True).click()
        self.assertTrue(self.page.locator(".paper-reader").is_visible())
        self.assertEqual(rail.get_by_label("消息", exact=True).input_value(), "保留这份草稿")
        self._wait_for_width(".run-inspector", 480)
        self._send("边读论文边讨论")
        rail.locator(".message-assistant").filter(has_text="来自 FastAPI：边读论文边讨论").wait_for()
        self.assertTrue(self.page.locator(".paper-reader").is_visible())
        self.page.locator(".run-status-button").click()
        rail.get_by_role("heading", name="本轮任务", exact=True).wait_for()
        rail.locator(".draggable-chat-tab button").click()
        rail.get_by_label("消息", exact=True).fill("第二份草稿")
        self.page.get_by_role("button", name="隐藏右侧栏", exact=True).click()
        self.page.get_by_role("button", name="显示右侧栏", exact=True).click()
        self.assertEqual(rail.get_by_label("消息", exact=True).input_value(), "第二份草稿")
        self._new_session()
        self.page.wait_for_function("document.querySelector('.composer textarea').value === ''")
        self.assertTrue(self.page.locator(".paper-reader").is_visible())
        self.page.locator(".session-item").filter(has_text="浏览器 E2E 会话").click()
        self.page.wait_for_function("document.querySelector('.composer textarea').value === '第二份草稿'")
        self.assertTrue(self.page.locator(".paper-reader").is_visible())
        self.page.get_by_role("separator", name="调整运行检查器宽度", exact=True).press("End")
        self._wait_for_width(".run-inspector", 800)
        self.assertGreaterEqual(self._pane_width(".conversation"), 480)
        self._set_viewport({"width": 1000, "height": 900})
        self._assert_no_horizontal_page_overflow()
        self.assertTrue(rail.get_by_role("button", name="发送", exact=True).is_visible())
        self._set_viewport({"width": 390, "height": 844})
        self._assert_no_horizontal_page_overflow()
        self.assertTrue(rail.get_by_label("消息", exact=True).is_visible())
        self._set_viewport({"width": 1700, "height": 1000})
        self._wait_for_width(".run-inspector", 800)
        self._drag_chat_tab(False)
        self.page.locator(".conversation .composer").wait_for()
        self.assertEqual(self.page.get_by_label("消息", exact=True).input_value(), "第二份草稿")
        self.assertTrue(self.page.evaluate("window.originalComposer === document.querySelector('.composer')"))
        self.assertEqual(self.page.locator(".composer").count(), 1)
        self.page.get_by_role("tab", name="docking-paper.pdf", exact=True).click()
        self.assertTrue(self.page.locator(".paper-reader").is_visible())

    def test_docked_chat_restores_placement_width_and_hidden_state_after_reload(self) -> None:
        self._set_viewport({"width": 1700, "height": 900})
        self._send("刷新后保留右侧对话")
        self.page.locator(".message-assistant").filter(has_text="来自 FastAPI：刷新后保留右侧对话").wait_for()
        self._drag_chat_tab(True)
        self.page.get_by_role("separator", name="调整运行检查器宽度", exact=True).press("End")
        self._wait_for_width(".run-inspector", 800)
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator(".right-workspace .message-assistant").filter(has_text="来自 FastAPI：刷新后保留右侧对话").wait_for()
        self._wait_for_width(".run-inspector", 800)
        self.assertEqual(self.page.locator(".conversation .draggable-chat-tab").count(), 0)
        self.page.get_by_role("button", name="隐藏右侧栏", exact=True).click()
        self.page.reload(wait_until="domcontentloaded")
        self.assertFalse(self.page.locator(".right-workspace").is_visible())
        self.page.locator(".session-item.active").click()
        self.page.locator(".right-workspace .composer").wait_for()
        self._wait_for_width(".run-inspector", 800)
        self._drag_chat_tab(False)
        self.page.wait_for_function("document.activeElement === document.querySelector('.conversation .draggable-chat-tab button')")
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator(".conversation .composer").wait_for()
        self.assertEqual(self.page.locator(".right-workspace .draggable-chat-tab").count(), 0)

    def test_paper_selections_attach_to_current_chat_without_writing_a_question(self) -> None:
        import fitz
        self._set_viewport({"width": 1440, "height": 900})
        with fitz.open() as document:
            document.new_page().insert_text((50, 80), "A selected passage.")
            document.new_page().insert_text((50, 80), "Figure on the second page")
            uploaded = self.page.request.post(f"{self.base_url}/api/v1/library/documents", multipart={
                "file": {"name": "selection.pdf", "mimeType": "application/pdf", "buffer": document.tobytes()},
            })
        self.assertEqual(uploaded.status, 201)
        self.page.get_by_role("button", name="论文库", exact=True).click()
        self.page.get_by_role("button", name="selection.pdf", exact=True).click()
        line = self.page.locator(".textLayer span").filter(has_text="A selected passage.").first
        line.wait_for()
        box = line.bounding_box()
        y = box["y"] + box["height"] / 2
        self.page.mouse.move(box["x"] + 0.5, y)
        self.page.mouse.down()
        self.page.mouse.move(box["x"] + box["width"] - 0.5, y, steps=8)
        self.page.mouse.up()
        composer = self.page.locator(".right-workspace .composer")
        chip = composer.locator(".paper-citation")
        chip.filter(has_text="A selected passage.").wait_for()
        question = composer.get_by_label("消息", exact=True)
        send = composer.get_by_role("button", name="发送", exact=True)
        self.assertEqual(question.input_value(), "")
        self.assertTrue(send.is_disabled())
        question.fill("这段话与第二页的图有什么联系？请引用 [S1]。")
        composer.get_by_role("button", name="＋ 加一处对照").click()
        self.page.get_by_role("button", name="下一页", exact=True).click()
        self.page.locator(".textLayer span").filter(has_text="Figure on the second page").first.wait_for()
        self.page.get_by_role("button", name="框选引用", exact=True).click()
        picker = self.page.locator(".paper-area-picker").bounding_box()
        self.page.mouse.move(picker["x"] + 25, picker["y"] + 30)
        self.page.mouse.down()
        self.page.mouse.move(picker["x"] + 170, picker["y"] + 95, steps=6)
        self.page.mouse.up()
        chip.locator("img").wait_for()
        self.assertEqual(chip.count(), 2)
        authored = "这段话与第二页的图有什么联系？请引用 [S1]。"
        self.assertEqual(question.input_value(), authored)
        self.assertEqual(self._state()["calls"], [])
        with self.page.expect_response(lambda r: r.url.endswith("/api/v1/runs") and r.status == 503):
            self.page.route("**/api/v1/runs", lambda route: route.fulfill(
                status=503, json={"detail": "fixture: offline"}), times=1)
            send.click()
        self.assertEqual(question.input_value(), authored)
        self.assertEqual(chip.count(), 2)
        send.click()
        self._wait_for_run_status("completed")
        self.assertEqual(chip.count(), 0)
        call, = self._state()["calls"]
        self.assertTrue(call["message"].startswith(authored + "\n\n"))
        self.assertIn("> A selected passage.", call["message"])
        self.assertIn("/2/area/", call["message"])
        # The fixture echoes the authored [S1] in the assistant reply.
        citation = self.page.locator(".message-assistant").get_by_role("link", name="S1", exact=True).first
        citation.hover()
        preview = self.page.get_by_role("tooltip")
        preview.get_by_text("selection.pdf · 第 1 页", exact=True).wait_for()
        self.assertTrue(preview.locator("img").get_attribute("src").startswith("data:image/png;base64,"))
        self.assertEqual(self.page.get_by_label("页码", exact=True).input_value(), "2")
        self.page.keyboard.press("Escape")
        preview.wait_for(state="hidden")
        citation.focus()
        preview.locator("img").wait_for()
        citation.click()
        self.page.wait_for_function("""document.querySelector('input[aria-label="页码"]').value === '1'""")
        self.page.locator(".paper-reference-highlight").first.wait_for()

    def test_saved_answer_with_a_retrieved_citation_opens_an_unopened_paper(self) -> None:
        import fitz
        from papers.selections import element_citations
        from infra.runtime_paths import RuntimePaths

        with fitz.open() as pdf:
            pdf.new_page()
            page = pdf.new_page()
            page.insert_text((70, 100), "The observed delay decreased.")
            box = list(page.search_for("The observed delay decreased.")[0])
            response = self.page.request.post(f"{self.base_url}/api/v1/library/documents", multipart={
                "file": {"name": "delay.pdf", "mimeType": "application/pdf", "buffer": pdf.tobytes()},
            })
        self.assertEqual(response.status, 201)
        document_id = response.json()["id"]
        links, _ = element_citations(RuntimePaths.from_root(self.tmp.name), {
            "document_id": document_id, "pages": [{"page": 2, "elements": [{"id": "result", "bbox": box}]}],
        })
        self.page.route("**/api/v1/sessions/*/messages", lambda route: route.fulfill(json=[
            {"role": "user", "content": "研究发现了什么？"},
            {"role": "assistant", "content": "实验中观察到的时延下降了。" + links["result"]},
        ]))
        self.page.reload()
        citation = self.page.locator(".message-assistant").get_by_role("link", name="第2页")
        citation.hover()
        preview = self.page.get_by_role("tooltip")
        preview.get_by_text("delay.pdf · 第 2 页", exact=True).wait_for()
        self.assertIn("observed delay", preview.locator("img").get_attribute("alt"))
        self.assertEqual(self.page.locator(".paper-reader").count(), 0)
        citation.click()
        self.page.locator(".textLayer span").filter(has_text="The observed delay decreased.").first.wait_for()
        self.assertEqual(self.page.get_by_label("页码", exact=True).input_value(), "2")
        self.page.locator(".paper-reference-highlight").first.wait_for()

    def test_reading_tools_open_from_paper_while_chat_is_docked(self) -> None:
        self._set_viewport({"width": 1440, "height": 900})
        import fitz
        with fitz.open() as document:
            document.new_page().insert_text((50, 80), "Every claim needs evidence.")
            document.new_page().insert_text((50, 80), "Hold this page for concurrent chat.")
            uploaded = self.page.request.post(f"{self.base_url}/api/v1/library/documents", multipart={
                "file": {"name": "reading-tools.pdf", "mimeType": "application/pdf", "buffer": document.tobytes()},
            })
        self.assertEqual(uploaded.status, 201)
        self.page.get_by_label("消息", exact=True).fill("保留对话草稿")
        self.page.get_by_role("button", name="论文库", exact=True).click()
        self.page.get_by_role("button", name="reading-tools.pdf", exact=True).click()
        self.page.locator(".textLayer span").first.wait_for()
        self._drag_chat_tab(True)
        self.page.get_by_role("button", name="随手记", exact=True).click()
        note = self.page.get_by_role("textbox", name="笔记正文", exact=True)
        note.fill("笔记与聊天分别保留")
        self.page.get_by_role("button", name="保存笔记", exact=True).click()
        self.page.locator(".note-save-status").filter(has_text="已保存").wait_for()
        self.page.locator(".right-workspace .draggable-chat-tab button").click()
        self.assertEqual(self.page.get_by_label("消息", exact=True).input_value(), "保留对话草稿")
        self.page.get_by_role("button", name="查看译文", exact=True).click()
        paragraph = self.page.locator(".paper-translation article").first
        paragraph.get_by_text("每个判断都需要证据。").wait_for()
        paragraph.hover()
        highlight = self.page.locator(".translation-highlight")
        highlight.wait_for()
        self.page.locator(".reader-toolbar").hover()
        highlight.wait_for(state="hidden")
        self.page.locator(".textLayer span").first.hover()
        self.page.locator(".paper-translation article.paired").wait_for()
        self.page.locator(".right-workspace .draggable-chat-tab button").click()
        self.assertEqual(self.page.get_by_label("消息", exact=True).input_value(), "保留对话草稿")
        self._send("翻译时继续聊天")
        self.page.locator(".message-assistant").filter(has_text="来自 FastAPI：翻译时继续聊天").wait_for()
        self.page.request.post(f"{self.base_url}/__e2e__/finish-translation")
        self.page.locator(".translation-toast").filter(has_text="翻译完成").wait_for()
        self.page.locator(".translation-toast button").first.click()
        self.page.locator("#run-inspector .translated-sheet [data-segment]").first.hover()
        self.page.locator(".translation-highlight").wait_for()
        self.page.get_by_role("button", name="下一页", exact=True).click()
        self.page.get_by_label("第 2 页中文 PDF").wait_for()
        pdf = self.page.request.get(f"{self.base_url}/api/v1/library/documents/{uploaded.json()['id']}/translation/file")
        self.assertEqual(pdf.status, 200)
        with fitz.open(stream=pdf.body(), filetype="pdf") as result:
            self.assertEqual(len(result), 2)
            self.assertIn("每个判断",result[1].get_text().replace("\n",""))

    def test_translation_renders_latex_and_scrolls_wide_equations(self) -> None:
        self._set_viewport({"width": 1440, "height": 1000})
        import fitz
        with fitz.open() as document:
            document.new_page().insert_text((50, 80), "Definition 1. Delay duration difference model.")
            uploaded = self.page.request.post(f"{self.base_url}/api/v1/library/documents", multipart={
                "file": {"name": "formula-translation.pdf", "mimeType": "application/pdf", "buffer": document.tobytes()},
            })
        self.assertEqual(uploaded.status, 201)
        translation = r"""定义 1。设 \(S_A\) 从发言者到节点 \(v_i\) 的延迟区间为 \([d^A_{v_i},D^A_{v_i}]\)。中心点的接近度定义为：

\[
\alpha_{v_i}(S_A,S_B)=\frac{1}{1+\left|\frac{D^A_{v_i}+d^A_{v_i}}{2}-\frac{D^B_{v_i}+d^B_{v_i}}{2}\right|}.\tag{1}
\]

类似地，延迟区间长度的接近度定义为：

\[
\beta_{v_i}(S_A,S_B)=\frac{1}{1+|(D^A_{v_i}-d^A_{v_i})-(D^B_{v_i}-d^B_{v_i})|}\qquad\text{for all pairs of sending nodes in the admissible set}.\tag{2}
\]

两者的值均介于 0 和 1 之间。"""
        self.page.route("**/translation?page=1", lambda route: route.fulfill(json={
            "page": 1, "state": "complete", "done": 1, "total": 1, "error": "",
            "segments": [{"id": "first", "kind": "text", "bounds": [.1,.1,.8,.2],
                          "source": "Definition 1.", "translation": translation}],
        }))
        self.page.get_by_role("button", name="论文库", exact=True).click()
        self.page.get_by_role("button", name="formula-translation.pdf", exact=True).click()
        self.page.get_by_role("button", name="查看译文", exact=True).click()
        self.page.get_by_role("button", name="段落文本", exact=True).click()
        self.page.locator(".translation-text .katex-display").first.wait_for()
        self.page.evaluate("document.fonts.ready")
        result = self.page.locator(".translation-text")
        self.assertEqual(result.locator(".katex-display").count(), 2)
        self.assertEqual(result.locator(".katex").count(), 5)
        self.assertEqual(result.locator(".tag").count(), 2)
        self.assertEqual(result.locator(".math-fallback, .katex-error").count(), 0)
        self.assertTrue(self.page.evaluate("document.fonts.check('16px KaTeX_Main')"))
        self.assertNotIn(r"\[", result.inner_text())
        self.assertNotIn(r"\(", result.inner_text())
        self.assertIn("两者的值均介于 0 和 1 之间。", result.inner_text())
        self.page.get_by_role("separator", name="调整运行检查器宽度").press("Home")
        self.assertTrue(result.evaluate("el => el.scrollWidth <= el.clientWidth + 1"))
        self._assert_no_horizontal_page_overflow()
        equation = result.locator(".katex-display").last
        self.assertTrue(equation.evaluate("el => el.scrollWidth > el.clientWidth"))
        self.assertGreater(equation.evaluate("el => { el.scrollLeft = el.scrollWidth; return el.scrollLeft; }"), 0)
        output = ROOT / "runtime" / "evaluations" / "translation-math"
        output.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(output / "sidebar-formulas.png"))
        result.screenshot(path=str(output / "translation-rendered.png"))
        self.page.get_by_role("button", name="译文加入笔记", exact=True).click()
        editor = self.page.get_by_role("textbox", name="笔记正文", exact=True)
        self.assertEqual(editor.input_value(), translation)
        self.page.get_by_role("button", name="保存笔记", exact=True).click()
        self.page.locator(".note-save-status").filter(has_text="已保存").wait_for()
        notes = self.page.request.get(f"{self.base_url}/api/v1/library/documents/{uploaded.json()['id']}/notes").json()
        self.assertEqual(notes[0]['content'], translation)

    def test_docking_during_session_load_keeps_the_current_content_tab(self) -> None:
        self._set_viewport({"width": 1440, "height": 900})
        self.page.locator(".session-item.active").wait_for()
        created = self.page.request.post(f"{self.base_url}/api/v1/sessions", data={"title": "延迟读取的会话"})
        session_id = created.json()["thread_id"]
        self.page.reload(wait_until="domcontentloaded")
        self.page.get_by_role("button", name="历史运行", exact=True).click()
        pending = []
        self.page.route(f"**/sessions/{session_id}/messages", lambda route: pending.append((route, route.fetch())))
        self.page.locator(".session-item").filter(has_text="延迟读取的会话").click()
        deadline = time.monotonic() + 5
        while not pending and time.monotonic() < deadline:
            self.page.wait_for_timeout(20)
        self.assertTrue(pending)
        self._drag_chat_tab(True)
        self.assertTrue(self.page.get_by_role("tab", name="运行中心", exact=True).get_attribute("aria-selected") == "true")
        route, response = pending.pop()
        route.fulfill(response=response)
        self.page.wait_for_function("!document.querySelector('.composer textarea').disabled")
        self.assertTrue(self.page.get_by_role("tab", name="运行中心", exact=True).get_attribute("aria-selected") == "true", "晚到的会话响应不能切换中央页面")
        self.assertTrue(self.page.locator(".right-workspace .composer").is_visible())

    def test_docking_keeps_history_scroll_position(self) -> None:
        self._set_viewport({"width": 1700, "height": 900})
        self.page.route("**/api/v1/sessions/*/messages", lambda route: route.fulfill(json=[
            {"role": "user", "content": "阅读历史消息"},
            {"role": "assistant", "content": "历史段落，保留阅读位置。\n\n" * 100},
        ]))
        self.page.reload(wait_until="domcontentloaded")
        listing = self.page.locator(".message-list")
        self.page.wait_for_function("document.querySelector('.message-list').scrollTop > 600")
        listing.hover()
        self.page.mouse.wheel(0, -600)
        self.page.wait_for_timeout(250)
        top = listing.evaluate("el => el.scrollTop")
        for name in ("论文库", "设置", "工作台"):
            self.page.get_by_role("button", name=name, exact=True).click()
        self.page.wait_for_timeout(150)
        self.assertAlmostEqual(listing.evaluate("el => el.scrollTop"), top, delta=3)
        self._drag_chat_tab(True)
        self.page.wait_for_timeout(250)
        self.assertAlmostEqual(listing.evaluate("el => el.scrollTop"), top, delta=3)
        self._drag_chat_tab(False)
        self.page.wait_for_timeout(250)
        self.assertAlmostEqual(listing.evaluate("el => el.scrollTop"), top, delta=3)

    def test_chat_docks_into_hidden_right_rail_and_empty_workbench(self) -> None:
        self._set_viewport({"width": 1440, "height": 900})
        self.page.get_by_role("button", name="隐藏运行检查器", exact=True).click()
        self._send("__slow__")
        self.page.locator(".message-assistant").filter(has_text="旧会话片段").wait_for()
        self._drag_chat_tab(True)
        self.page.locator(".right-workspace .message-assistant").filter(has_text="慢请求完成").wait_for()
        self.assertTrue(self.page.locator(".dock-empty-workbench").is_visible())
        self._wait_for_width(".session-sidebar", 240)
        self._wait_for_width(".run-inspector", 480)
        self.assertEqual(len(self._state()["calls"]), 1)
        tab = self.page.locator(".right-workspace .draggable-chat-tab button")
        tab.press("Alt+Shift+ArrowLeft")
        self.page.locator(".conversation .composer").wait_for()
        self.page.locator(".conversation .draggable-chat-tab button").press("Alt+Shift+ArrowRight")
        self.page.locator(".right-workspace .composer").wait_for()

    def test_sidebars_resize_show_hide_and_restore_saved_preferences(self) -> None:
        self._set_viewport({"width": 1496, "height": 900})
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
        self._wait_for_width(".run-inspector", 540)
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
        self._wait_for_width(".run-inspector", 540)
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
        self._set_viewport({"width": 1496, "height": 900})
        self._wait_for_width(".session-sidebar", 420)
        self._wait_for_width(".run-inspector", 540)

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
