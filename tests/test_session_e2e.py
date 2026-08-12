"""真实浏览器回归：新会话绝不能在第一条消息时重新出现旧历史。

运行：
    pip install -r requirements-dev.txt
    python -m playwright install chromium
    python -m unittest tests.test_session_e2e

测试启动临时的本地 SSE 模拟服务，不读取用户 ``runtime``，也不会访问真实模型 API。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from tests.browser import launch_chromium


ROOT = Path(__file__).resolve().parents[1]


try:
    from playwright.sync_api import sync_playwright
except ImportError:  # 本地核心测试不强制安装浏览器；CI 的 e2e job 会安装。
    sync_playwright = None


class _SlowSSEHandler(BaseHTTPRequestHandler):
    """第一段 token 立即返回，第二段刻意延迟以暴露会话切换竞态。"""

    def do_POST(self):  # noqa: N802 - HTTP handler API
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        first = {"choices": [{"delta": {"content": "旧会话片段"}}]}
        try:
            self.wfile.write(f"data: {json.dumps(first)}\n\n".encode("utf-8"))
            self.wfile.flush()
            time.sleep(2.5)
            second = {"choices": [{"delta": {"content": "（不应出现在新会话）"}}]}
            self.wfile.write(f"data: {json.dumps(second)}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_args):
        pass


@unittest.skipUnless(sync_playwright, "安装 requirements-dev.txt 后运行浏览器 E2E")
class TestSessionIsolationE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mock_server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowSSEHandler)
        self.mock_thread = threading.Thread(
            target=self.mock_server.serve_forever, daemon=True,
        )
        self.mock_thread.start()

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.ui_port = sock.getsockname()[1]

        env = os.environ.copy()
        env.update({
            "DEEPSEEK_API_KEY": "e2e-test-key",
            "DEEPSEEK_API_URL": (
                f"http://127.0.0.1:{self.mock_server.server_port}/chat/completions"
            ),
            "PYTHONUNBUFFERED": "1",
        })
        self.app = subprocess.Popen(
            [
                sys.executable, "web_ui.py", "--host", "127.0.0.1",
                "--port", str(self.ui_port), "--data-dir", self.tmp.name, "--no-rag",
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._wait_until_ready()
        self.playwright = sync_playwright().start()
        self.browser = launch_chromium(self.playwright)
        self.page = self.browser.new_page()
        self.page.goto(f"http://127.0.0.1:{self.ui_port}/", wait_until="domcontentloaded")

    def tearDown(self):
        if hasattr(self, "browser"):
            self.browser.close()
        if hasattr(self, "playwright"):
            self.playwright.stop()
        if hasattr(self, "app"):
            self.app.terminate()
            try:
                self.app.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.app.kill()
        if hasattr(self, "mock_server"):
            self.mock_server.shutdown()
            self.mock_server.server_close()
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def _wait_until_ready(self):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.app.poll() is not None:
                self.fail("E2E Web UI 启动失败")
            try:
                with urlopen(f"http://127.0.0.1:{self.ui_port}/", timeout=0.5) as response:
                    if response.status == 200:
                        return
            except OSError:
                time.sleep(0.1)
        self.fail("E2E Web UI 在 20 秒内未就绪")

    def _send(self, text: str):
        self.page.locator("textarea").first.fill(text)
        # Gradio exposes the submit control with the fixed aria-label "Submit",
        # even when its visible, localized label is "发送".  Locate the user-facing
        # text so this browser regression stays aligned with the actual UI.
        self.page.get_by_text("发送", exact=True).click()

    def test_new_session_first_message_never_restores_old_stream(self):
        new_session = self.page.get_by_role("button", name="＋ 新建会话")
        new_session.click()

        self._send("慢请求")
        self.page.get_by_text("旧会话片段").wait_for(timeout=8_000)

        new_session.click()
        self.page.get_by_text("旧会话片段").wait_for(state="detached", timeout=3_000)

        self._send("/help")
        self.page.get_by_text("命令列表").wait_for(timeout=5_000)
        self.assertEqual(self.page.get_by_text("旧会话片段").count(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
