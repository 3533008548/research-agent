"""浏览器级聊天请求失效保护。

Gradio 的聊天回调是流式生成器。用户在旧请求仍在运行时切换或新建会话，旧生成器
后续产生的内容不能再写回当前聊天框；否则视觉上会出现“新会话串入旧历史”。
"""

from __future__ import annotations

import threading


class BrowserRunGuard:
    """按 Gradio 浏览器 session_hash 管理当前有效的流式请求。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, tuple[int, threading.Event]] = {}

    def begin(self, browser_id: str) -> int:
        """开始一次请求，并使同一浏览器较早的请求失效。"""
        with self._lock:
            previous = self._runs.get(browser_id)
            if previous:
                previous[1].set()
                generation = previous[0] + 1
            else:
                generation = 1
            self._runs[browser_id] = (generation, threading.Event())
            return generation

    def invalidate(self, browser_id: str) -> None:
        """在切换、新建或删除会话时使当前流式请求停止回写。"""
        with self._lock:
            previous = self._runs.get(browser_id)
            if previous:
                previous[1].set()
                generation = previous[0] + 1
            else:
                generation = 1
            # 保留递增代次，确保已在运行的生成器立即判定为过期。
            self._runs[browser_id] = (generation, threading.Event())

    def is_current(self, browser_id: str, generation: int) -> bool:
        with self._lock:
            run = self._runs.get(browser_id)
            return run is not None and run[0] == generation and not run[1].is_set()

    def cancellation_event(self, browser_id: str, generation: int) -> threading.Event:
        """返回本次请求的取消令牌；过期请求得到已触发的令牌。"""
        with self._lock:
            run = self._runs.get(browser_id)
            if run is not None and run[0] == generation:
                return run[1]
        cancelled = threading.Event()
        cancelled.set()
        return cancelled

    def finish(self, browser_id: str, generation: int) -> None:
        """仅清理仍属于本次请求的标记，不能误清理较新的请求。"""
        with self._lock:
            run = self._runs.get(browser_id)
            if run is not None and run[0] == generation:
                self._runs.pop(browser_id, None)
