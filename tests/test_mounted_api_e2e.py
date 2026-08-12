"""Browser regression tests for Gradio mounted as a FastAPI HTTP client.

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
    """Gradio must create and cancel runs through mounted FastAPI routes."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
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

    def tearDown(self) -> None:
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

    def _wait_for_run_status(self, expected: str) -> dict:
        deadline = time.monotonic() + 8
        latest: dict = {}
        while time.monotonic() < deadline:
            state = self._state()
            if state["runs"]:
                latest = state["runs"][-1]
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

    def test_switching_session_cancels_old_fastapi_run(self) -> None:
        self._send("__slow__")
        self.page.get_by_text("旧会话片段", exact=True).wait_for(timeout=8_000)

        self._new_session()
        self._wait_for_run_status("cancelled")
        self.page.get_by_text("旧会话片段", exact=True).wait_for(state="detached", timeout=3_000)

        self._send("新会话请求")
        self.page.get_by_text("来自 FastAPI：新会话请求", exact=True).wait_for(timeout=8_000)
        state = self._state()
        self.assertEqual(len(state["calls"]), 2)
        self.assertNotEqual(state["calls"][0]["session_id"], state["calls"][1]["session_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
