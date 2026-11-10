"""主模型调用的同模型韧性层：有界并发、重试、总截止时间与熔断。

这个模块刻意不实现模型降级。发生故障时只会重试当前配置的模型，失败后把
可操作错误交给上层 UI，而不会在用户不知情的情况下改变回答质量或成本。
"""

from __future__ import annotations

import random
import re
import threading
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from enum import IntEnum
from typing import Callable

import requests

from cancellation import RequestCancelledError, raise_if_cancelled
from resilience import CircuitBreaker
from run_contract import safe_error_details


class LLMClientError(RuntimeError):
    """主模型请求未能在既定策略内完成。"""

    def __init__(self, message: str, *, error_details: dict | None = None):
        super().__init__(message)
        # Diagnostic fields have a separate allow-list from human-readable
        # messages. Never promote a response body or exception string here.
        self.error_details = safe_error_details(error_details)


class LLMQueueFullError(LLMClientError):
    pass


class LLMCircuitOpenError(LLMClientError):
    pass


class LLMRequestTimeoutError(LLMClientError):
    pass


class LLMRequestFailedError(LLMClientError):
    pass


class LLMStreamError(requests.RequestException):
    """A protocol-level incomplete response, without retaining upstream text."""

    def __init__(self, code="LLM_STREAM_INTERRUPTED", *, retryable=True):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class _RetryableHTTPError(Exception):
    def __init__(self, response: requests.Response):
        self.response = response
        self.status_code = response.status_code
        try:
            self.body = response.text[:200].replace("\n", " ")
        except Exception:
            self.body = ""
        super().__init__(f"HTTP {self.status_code}")


def _safe_cause_type(error: BaseException | None) -> str:
    """Keep the useful transport category without retaining exception text."""
    names = {
        "ReadTimeoutError": "ReadTimeout", "ConnectTimeoutError": "ConnectTimeout",
        "ReadTimeout": "ReadTimeout", "ConnectTimeout": "ConnectTimeout",
        "Timeout": "Timeout", "ConnectionError": "ConnectionError",
        "ChunkedEncodingError": "ChunkedEncodingError", "ProtocolError": "ChunkedEncodingError",
        "HTTPError": "HTTPError", "_RetryableHTTPError": "HTTPError",
        "RequestException": "RequestException", "OSError": "OSError",
        "LLMStreamError": "RequestException",
    }
    seen: set[int] = set()
    result = ""
    while error is not None and id(error) not in seen and len(seen) < 8:
        seen.add(id(error))
        result = names.get(type(error).__name__, result)
        nested = error.__cause__ or error.__context__
        if nested is None:
            nested = next((item for item in error.args if isinstance(item, BaseException)), None)
        error = nested
    return result


class RequestPriority(IntEnum):
    """Admission classes for requests sharing one model client."""

    INTERACTIVE = 0
    RESEARCH = 1
    VERIFY = 2
    SUMMARY = 3


@dataclass(frozen=True)
class RequestPolicy:
    """Per-call limits without changing the configured model."""

    purpose: str = "chat"
    priority: RequestPriority = RequestPriority.INTERACTIVE
    deadline_seconds: float | None = None
    max_retries: int | None = None
    # Optional quality stages use short budgets. Their timeout can reflect queue
    # pressure rather than a bad main-model endpoint, so it must not open the
    # shared circuit used by interactive requests.
    counts_toward_circuit: bool = True
    read_timeout_seconds: float | None = None


@dataclass
class RequestBudget:
    """一次模型调用的共享预算，覆盖排队、重试与流式正文。"""

    deadline_at: float
    max_retries: int
    purpose: str = "chat"
    priority: RequestPriority = RequestPriority.INTERACTIVE
    counts_toward_circuit: bool = True
    read_timeout_seconds: float | None = None
    retries_used: int = 0
    queue_wait_seconds: float = 0.0
    attempts: int = 0
    started_at: float = field(default_factory=time.monotonic)
    duration_seconds: float | None = None
    phase: str = "queue"
    last_http_status: int | None = None
    last_request_id: str = ""
    last_cause_type: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def remaining_seconds(self) -> float:
        return self.deadline_at - time.monotonic()

    def reserve_retry(self) -> bool:
        """预留一次重试。HTTP 首包失败和 SSE 正文失败共用同一额度。"""
        with self._lock:
            if self.retries_used >= self.max_retries:
                return False
            self.retries_used += 1
            return True

    def record_queue_wait(self, seconds: float) -> None:
        with self._lock:
            self.queue_wait_seconds += max(0.0, seconds)

    def record_attempt(self) -> None:
        with self._lock:
            self.attempts += 1
            self.phase = "connect"

    def note_response(self, response, *, api_key: str = "") -> None:
        """Remember only response metadata, including for interrupted HTTP 200 SSE."""
        status = getattr(response, "status_code", None)
        headers = getattr(response, "headers", {}) or {}
        request_id = ""
        for key, value in headers.items():
            if str(key).lower() not in {"request-id", "x-request-id"}:
                continue
            if (
                isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}", value)
                and not value.lower().startswith(("sk-", "bearer"))
                and (not api_key or api_key not in value)
            ):
                request_id = value
                break
        with self._lock:
            self.last_http_status = status if isinstance(status, int) and not isinstance(status, bool) else None
            self.last_request_id = request_id
            self.phase = "response_headers"
            self.last_cause_type = ""

    def note_failure(self, error: BaseException, *, phase: str | None = None) -> None:
        cause = _safe_cause_type(error)
        with self._lock:
            if cause:
                self.last_cause_type = cause
            if phase is not None:
                self.phase = phase

    def error_details(self, code: str, *, exception_type: str, phase: str | None = None) -> dict:
        with self._lock:
            duration = self.duration_seconds
            if duration is None:
                duration = max(0.0, self.deadline_at - self.started_at)
            result = {
                "code": code, "phase": phase or self.phase,
                "exception_type": exception_type,
                "elapsed_ms": round(max(0.0, time.monotonic() - self.started_at) * 1000, 1),
                "deadline_ms": round(max(0.0, duration) * 1000, 1),
                "queue_wait_ms": round(self.queue_wait_seconds * 1000, 1),
                "attempts": self.attempts,
                # reserve_retry is a policy reservation, not an HTTP attempt.
                "retries_used": max(0, self.attempts - 1),
            }
            if self.last_http_status is not None:
                result["http_status"] = self.last_http_status
            if self.last_request_id:
                result["request_id"] = self.last_request_id
            if self.last_cause_type:
                result["cause_type"] = self.last_cause_type
            return safe_error_details(result)

    def metrics(self) -> dict[str, int | str]:
        with self._lock:
            return {
                "purpose": self.purpose,
                "priority": self.priority.name.lower(),
                "counts_toward_circuit": self.counts_toward_circuit,
                "queue_wait_ms": round(self.queue_wait_seconds * 1000),
                "attempts": self.attempts,
                "retries_used": max(0, self.attempts - 1),
            }


class LLMClient:
    """共享的主模型 HTTP 客户端，不改变模型，只管理失败和拥塞。"""

    def __init__(
        self,
        api_key: str,
        api_url: str,
        *,
        max_concurrency: int = 4,
        interactive_reserved_slots: int = 1,
        low_priority_max_concurrency: int = 1,
        queue_size: int = 20,
        connect_timeout_seconds: float = 3.05,
        read_timeout_seconds: float = 30,
        request_deadline_seconds: float = 180,
        max_retries: int = 2,
        circuit_failure_threshold: int = 3,
        circuit_recovery_seconds: int = 120,
    ):
        self.api_key = api_key
        self.api_url = api_url
        self.connect_timeout_seconds = connect_timeout_seconds
        self.read_timeout_seconds = read_timeout_seconds
        self.request_deadline_seconds = request_deadline_seconds
        self.max_retries = max_retries
        self.max_concurrency = max(1, int(max_concurrency))
        self.interactive_reserved_slots = min(
            max(0, int(interactive_reserved_slots)), self.max_concurrency - 1,
        )
        self._slots = threading.BoundedSemaphore(self.max_concurrency)
        self._background_slots = threading.BoundedSemaphore(
            self.max_concurrency - self.interactive_reserved_slots
        )
        self.low_priority_max_concurrency = min(
            max(1, int(low_priority_max_concurrency)),
            self.max_concurrency - self.interactive_reserved_slots,
        )
        self._low_priority_slots = threading.BoundedSemaphore(
            self.low_priority_max_concurrency
        )
        self._queue_size = queue_size
        self._waiting = 0
        self._lock = threading.RLock()
        self._circuit = CircuitBreaker(
            failure_threshold=circuit_failure_threshold,
            recovery_seconds=circuit_recovery_seconds,
        )

    def post(
        self,
        payload: dict,
        *,
        stream: bool = False,
        on_status: Callable[[str], None] | None = None,
        budget: RequestBudget | None = None,
        policy: RequestPolicy | None = None,
        cancel_event: threading.Event | None = None,
    ) -> requests.Response:
        """提交同模型请求。

        ``budget`` 由同一轮模型调用的首包、流式正文和后续重放共享，因而不会因
        排队或流式断开而重置总截止时间或重试计数。
        """
        budget = budget or self.new_request_budget(policy)
        raise_if_cancelled(cancel_event, "模型请求已取消")
        if budget.remaining_seconds() <= 0:
            raise LLMRequestTimeoutError(
                "模型请求在总截止时间内未完成",
                error_details=budget.error_details("LLM_DEADLINE_EXCEEDED", exception_type="LLMRequestTimeoutError"),
            )
        if not self._circuit.allow_request():
            raise LLMCircuitOpenError(
                "模型服务暂不可用，请稍后重试"
                f"（约 {self._circuit.remaining_seconds()} 秒后恢复）",
                error_details=budget.error_details("LLM_CIRCUIT_OPEN", exception_type="LLMCircuitOpenError"),
            )

        budget.phase = "queue"
        try:
            acquired, background_slot_owned, low_priority_slot_owned = self._acquire_slot(
                on_status,
                budget,
                cancel_event,
            )
        except LLMQueueFullError as exc:
            self._circuit.release_probe()
            exc.error_details = budget.error_details("LLM_QUEUE_FULL", exception_type="LLMQueueFullError")
            raise
        except RequestCancelledError:
            self._circuit.release_probe()
            raise
        if not acquired:
            self._circuit.release_probe()
            raise LLMRequestTimeoutError(
                "请求在排队期间超时，请稍后重试",
                error_details=budget.error_details("LLM_QUEUE_TIMEOUT", exception_type="LLMRequestTimeoutError"),
            )

        last_error: Exception | None = None
        response: requests.Response | None = None
        release_slot = True
        release_background_slot = background_slot_owned
        release_low_priority_slot = low_priority_slot_owned
        try:
            while True:
                raise_if_cancelled(cancel_event, "模型请求已取消")
                remaining = budget.remaining_seconds()
                if remaining <= 0:
                    break
                if on_status:
                    on_status(
                        "🤖 正在请求模型..." if budget.retries_used == 0
                        else f"🔄 模型请求重试 {budget.retries_used}/{budget.max_retries}..."
                    )
                try:
                    budget.record_attempt()
                    response = requests.post(
                        self.api_url,
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                        timeout=(
                            min(self.connect_timeout_seconds, remaining),
                            min(budget.read_timeout_seconds or self.read_timeout_seconds, remaining),
                        ),
                        stream=stream,
                    )
                    budget.note_response(response, api_key=self.api_key)
                    raise_if_cancelled(cancel_event, "模型请求已取消")
                    if response.status_code == 429 or response.status_code >= 500:
                        raise _RetryableHTTPError(response)
                    response.raise_for_status()
                    # 流式响应的 HTTP 200 只代表首包到达；必须读到 [DONE] 才能
                    # 清除熔断失败计数，且并发槽要一直持有到正文消费结束。
                    if stream:
                        setattr(response, "_llm_stream_slot_owned", True)
                        setattr(response, "_llm_background_slot_owned", background_slot_owned)
                        setattr(response, "_llm_low_priority_slot_owned", low_priority_slot_owned)
                        setattr(
                            response, "_llm_stream_counts_toward_circuit",
                            budget.counts_toward_circuit,
                        )
                        release_slot = False
                        release_background_slot = False
                        release_low_priority_slot = False
                    elif budget.counts_toward_circuit:
                        # Optional quality stages share the transport but must
                        # not alter the health signal that governs interactive
                        # traffic. A successful Curator must not close a
                        # circuit opened by failed chat requests.
                        self._circuit.record_success()
                    else:
                        self._circuit.release_probe()
                    if on_status:
                        on_status("🤖 模型响应中...")
                    return response
                except (_RetryableHTTPError, requests.Timeout, requests.ConnectionError) as exc:
                    if cancel_event is not None and cancel_event.is_set():
                        if isinstance(exc, _RetryableHTTPError):
                            self._close_response(exc.response)
                        raise RequestCancelledError("模型请求已取消") from exc
                    last_error = exc
                    budget.note_failure(
                        exc, phase="response_headers" if isinstance(exc, _RetryableHTTPError) or _safe_cause_type(exc) == "ReadTimeout" else "connect",
                    )
                    if isinstance(exc, _RetryableHTTPError):
                        self._close_response(exc.response)
                    if not budget.reserve_retry():
                        break
                    delay = self._retry_delay(exc, budget)
                    if delay is None:
                        break
                    budget.phase = "retry"
                    if on_status:
                        on_status(f"⏳ 请求失败，{delay:.1f}s 后重试...")
                    if cancel_event is not None and cancel_event.wait(delay):
                        raise RequestCancelledError("模型请求已取消")
                    if cancel_event is None:
                        time.sleep(delay)
                except requests.RequestException as exc:
                    # 4xx（除 429）通常是鉴权、参数或请求格式问题，重试没有意义。
                    self._record_or_release_circuit(budget, failed=True)
                    budget.note_failure(exc)
                    raise LLMRequestFailedError(
                        self._format_request_error(exc),
                        error_details=budget.error_details(
                            "LLM_HTTP_ERROR" if isinstance(exc, requests.HTTPError) else "LLM_REQUEST_FAILED",
                            exception_type="LLMRequestFailedError",
                        ),
                    ) from exc
                except RequestCancelledError:
                    if response is not None:
                        self._close_response(response)
                    self._circuit.release_probe()
                    raise
                except BaseException:
                    # 状态回调取消等异常发生在已拿到 SSE 首包之后时，也必须归还槽位。
                    if stream and not release_slot and response is not None:
                        self.finish_stream(response, success=False)
                    else:
                        self._record_or_release_circuit(budget, failed=True)
                    raise

            if last_error is None:
                self._circuit.release_probe()
                raise LLMRequestTimeoutError(
                    "模型请求在总截止时间内未完成",
                    error_details=budget.error_details("LLM_DEADLINE_EXCEEDED", exception_type="LLMRequestTimeoutError"),
                )

            self._record_or_release_circuit(budget, failed=True)
            if isinstance(last_error, (requests.Timeout, requests.ConnectionError)):
                if budget.remaining_seconds() <= 0:
                    code = "LLM_DEADLINE_EXCEEDED"
                elif budget.last_cause_type == "ConnectTimeout":
                    code = "LLM_CONNECT_TIMEOUT"
                elif isinstance(last_error, requests.Timeout) or budget.last_cause_type == "ReadTimeout":
                    code = "LLM_RESPONSE_TIMEOUT"
                else:
                    code = "LLM_CONNECTION_ERROR"
                raise LLMRequestTimeoutError(
                    "模型服务连接或响应超时；未切换模型，请稍后点击重试",
                    error_details=budget.error_details(
                        code,
                        exception_type="LLMRequestTimeoutError",
                    ),
                ) from last_error
            raise LLMRequestFailedError(
                self._format_request_error(last_error),
                error_details=budget.error_details("LLM_HTTP_ERROR", exception_type="LLMRequestFailedError"),
            ) from last_error
        finally:
            if release_slot:
                self._slots.release()
            if release_background_slot:
                self._background_slots.release()
            if release_low_priority_slot:
                self._low_priority_slots.release()

    def new_request_budget(
        self,
        policy: RequestPolicy | None = None,
        *,
        deadline_seconds: float | None = None,
        max_retries: int | None = None,
    ) -> RequestBudget:
        """Create a policy-scoped end-to-end budget for one model call."""
        policy = policy or RequestPolicy()
        duration = (
            deadline_seconds
            if deadline_seconds is not None
            else policy.deadline_seconds
        )
        if duration is None:
            duration = self.request_deadline_seconds
        retries = max_retries if max_retries is not None else policy.max_retries
        if retries is None:
            retries = self.max_retries
        started_at = time.monotonic()
        return RequestBudget(
            deadline_at=started_at + max(0.0, duration),
            max_retries=max(0, int(retries)),
            started_at=started_at,
            duration_seconds=max(0.0, duration),
            purpose=policy.purpose,
            priority=policy.priority,
            counts_toward_circuit=policy.counts_toward_circuit,
            read_timeout_seconds=policy.read_timeout_seconds,
        )

    def stream_error(self, budget: RequestBudget, error: BaseException, response=None) -> LLMRequestTimeoutError:
        """Describe a stream failure before any replay can overwrite its cause."""
        if response is not None:
            budget.note_response(response, api_key=self.api_key)
        budget.note_failure(error, phase="stream_read")
        cause = budget.last_cause_type
        code = "LLM_STREAM_TIMEOUT" if cause in {"ReadTimeout", "ConnectTimeout", "Timeout"} else "LLM_STREAM_INTERRUPTED"
        if isinstance(error, LLMStreamError):
            code = error.code
        if budget.remaining_seconds() <= 0:
            code = "LLM_DEADLINE_EXCEEDED"
        return LLMRequestTimeoutError(
            "模型流式响应中断；未切换模型，请稍后点击重试",
            error_details=budget.error_details(code, exception_type="LLMRequestTimeoutError"),
        )

    def prepare_stream_read(
        self,
        response: requests.Response,
        budget: RequestBudget,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        """在读取下一条 SSE 事件前收紧 socket 超时，避免正文越过总截止时间。"""
        raise_if_cancelled(cancel_event, "模型流式响应已取消")
        budget.phase = "stream_read"
        remaining = budget.remaining_seconds()
        if remaining <= 0:
            return False
        timeout = max(0.001, min(budget.read_timeout_seconds or self.read_timeout_seconds, remaining))
        raw = getattr(response, "raw", None)
        # requests / urllib3 在不同版本中的 socket 层级略有差异；找不到时仍保留
        # requests.post 设置的初始读超时，下一次收到事件后会再次检查 deadline。
        candidates = (
            getattr(getattr(raw, "_connection", None), "sock", None),
            getattr(
                getattr(getattr(raw, "_fp", None), "fp", None), "raw", None
            ),
        )
        for candidate in candidates:
            sock = getattr(candidate, "_sock", candidate)
            if hasattr(sock, "settimeout"):
                try:
                    sock.settimeout(timeout)
                    break
                except OSError:
                    continue
        return True

    def finish_stream(
        self,
        response: requests.Response,
        *,
        success: bool,
        cancelled: bool = False,
    ) -> None:
        """关闭 SSE 响应、释放并发槽，并在完整结束时才记录成功。"""
        with self._lock:
            if not getattr(response, "_llm_stream_slot_owned", False):
                return
            setattr(response, "_llm_stream_slot_owned", False)
        try:
            self._close_response(response)
        finally:
            self._slots.release()
            if getattr(response, "_llm_background_slot_owned", False):
                setattr(response, "_llm_background_slot_owned", False)
                self._background_slots.release()
            if getattr(response, "_llm_low_priority_slot_owned", False):
                setattr(response, "_llm_low_priority_slot_owned", False)
                self._low_priority_slots.release()
        if success and getattr(response, "_llm_stream_counts_toward_circuit", True):
            self._circuit.record_success()
        elif cancelled:
            self._circuit.release_probe()
        elif getattr(response, "_llm_stream_counts_toward_circuit", True):
            self._circuit.record_failure()
        else:
            self._circuit.release_probe()

    def _record_or_release_circuit(self, budget: RequestBudget, *, failed: bool) -> None:
        if failed and budget.counts_toward_circuit:
            self._circuit.record_failure()
        else:
            self._circuit.release_probe()

    def _acquire_slot(
        self,
        on_status: Callable[[str], None] | None,
        budget: RequestBudget,
        cancel_event: threading.Event | None = None,
    ) -> tuple[bool, bool, bool]:
        """Acquire global capacity and priority-specific admission permits.

        Background calls can never consume the interactive-reserved capacity.
        This gives new chat requests an admission guarantee even when verify,
        summaries, or future research workers are already busy.
        """
        raise_if_cancelled(cancel_event, "模型排队已取消")
        is_background = budget.priority > RequestPriority.INTERACTIVE
        is_low_priority = budget.priority >= RequestPriority.VERIFY
        background_owned = False
        low_priority_owned = False

        if is_low_priority:
            low_priority_owned = self._low_priority_slots.acquire(blocking=False)
        if is_background and (not is_low_priority or low_priority_owned):
            background_owned = self._background_slots.acquire(blocking=False)
        if (not is_low_priority or low_priority_owned) and (
            not is_background or background_owned
        ) and self._slots.acquire(blocking=False):
            return True, background_owned, low_priority_owned
        if background_owned:
            self._background_slots.release()
            background_owned = False
        if low_priority_owned:
            self._low_priority_slots.release()
            low_priority_owned = False

        with self._lock:
            if self._waiting >= self._queue_size:
                raise LLMQueueFullError("模型请求过多，队列已满，请稍后再试")
            self._waiting += 1
            position = self._waiting
        queue_started = time.monotonic()
        acquired = False
        try:
            if on_status:
                on_status(f"⏳ 模型繁忙，正在排队（前方约 {position} 个请求）...")
            while budget.remaining_seconds() > 0:
                raise_if_cancelled(cancel_event, "模型排队已取消")
                timeout = min(0.2, budget.remaining_seconds())
                if is_low_priority and not low_priority_owned:
                    low_priority_owned = self._low_priority_slots.acquire(timeout=timeout)
                    if not low_priority_owned:
                        continue
                if is_background and not background_owned:
                    background_owned = self._background_slots.acquire(timeout=timeout)
                    if not background_owned:
                        continue
                if self._slots.acquire(timeout=min(0.2, budget.remaining_seconds())):
                    acquired = True
                    budget.record_queue_wait(time.monotonic() - queue_started)
                    return True, background_owned, low_priority_owned
            budget.record_queue_wait(time.monotonic() - queue_started)
            return False, background_owned, low_priority_owned
        finally:
            if background_owned and not acquired:
                self._background_slots.release()
            if low_priority_owned and not acquired:
                self._low_priority_slots.release()
            with self._lock:
                self._waiting -= 1

    def _retry_delay(self, error: Exception, budget: RequestBudget) -> float | None:
        remaining = budget.remaining_seconds()
        retry_after = self._retry_after_seconds(error)
        # full jitter 防止同一时间失败的请求再次同时打到 API。
        base_delay = retry_after if retry_after is not None else min(
            4.0, 0.5 * (2 ** max(0, budget.retries_used - 1))
        )
        delay = max(0.0, base_delay + random.uniform(0, min(0.5, base_delay / 2)))
        return delay if delay < remaining else None

    @staticmethod
    def _close_response(response: requests.Response) -> None:
        try:
            response.close()
        except Exception:
            pass

    @staticmethod
    def _retry_after_seconds(error: Exception) -> float | None:
        if not isinstance(error, _RetryableHTTPError) or error.response.status_code != 429:
            return None
        value = error.response.headers.get("Retry-After", "")
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            try:
                return max(0.0, (parsedate_to_datetime(value).timestamp() - time.time()))
            except (TypeError, ValueError, IndexError):
                return None

    @staticmethod
    def _format_request_error(error: Exception | None) -> str:
        if isinstance(error, _RetryableHTTPError):
            return f"模型 API 返回 {error.status_code}: {error.body or '服务暂不可用'}"
        if isinstance(error, requests.HTTPError) and error.response is not None:
            response = error.response
            try:
                body = response.text[:500].replace("\n", " ").strip()
            except Exception:
                body = ""
            return (
                f"模型 API 返回 {response.status_code}: "
                f"{body or '未返回错误详情'}"
            )
        if error:
            return f"模型请求失败: {type(error).__name__}: {error}"
        return "模型请求在总截止时间内未完成"
