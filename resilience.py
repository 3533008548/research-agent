"""外部依赖的线程安全熔断器。"""

from __future__ import annotations

import threading
import time


class CircuitBreaker:
    """三态熔断器：closed -> open -> half_open。

    熔断窗口结束后只放行一个探测请求，避免服务刚恢复时被并发请求再次压垮。
    调用方必须为每个已放行请求调用 ``record_success`` 或 ``record_failure``。
    """

    def __init__(self, failure_threshold: int = 2, recovery_seconds: int = 120):
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._failures = 0
        self._open_until = 0.0
        self._state = "closed"
        self._probe_in_flight = False
        self._lock = threading.RLock()

    def allow_request(self) -> bool:
        with self._lock:
            if self._state == "closed":
                return True
            if self._state == "open":
                if time.monotonic() < self._open_until:
                    return False
                self._state = "half_open"

            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._open_until = 0.0
            self._state = "closed"
            self._probe_in_flight = False

    def record_failure(self) -> None:
        with self._lock:
            if self._state == "half_open":
                self._open_until = time.monotonic() + self.recovery_seconds
                self._state = "open"
                self._probe_in_flight = False
                return
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._open_until = time.monotonic() + self.recovery_seconds
                self._state = "open"
            self._probe_in_flight = False

    def release_probe(self) -> None:
        """释放未真正发出的半开探测资格（例如本地排队超时）。"""
        with self._lock:
            if self._state == "half_open":
                self._probe_in_flight = False

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def remaining_seconds(self) -> int:
        with self._lock:
            return max(0, int(self._open_until - time.monotonic()))
