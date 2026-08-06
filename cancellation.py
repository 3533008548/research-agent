"""协作式请求取消原语。

Python 线程不能安全地被强制终止；这里使用 ``threading.Event`` 在排队、重试、
流式读取和工具节点之间尽快退出，同时保证连接和并发槽能正常释放。
"""

from __future__ import annotations

import threading


class RequestCancelledError(RuntimeError):
    """调用方主动取消了本轮 Agent 请求，而不是模型或工具发生故障。"""


def raise_if_cancelled(
    cancel_event: threading.Event | None,
    message: str = "请求已取消",
) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise RequestCancelledError(message)
