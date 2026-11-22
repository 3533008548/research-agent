"""Bounded tool failures shared by model input, verification and safe run traces."""
from __future__ import annotations

import json
import re

from runs.contract import safe_error_details

ERROR_HEADER = '❌ [tool_error] '
REASONS = {
    'LLM_CONNECT_TIMEOUT': '连接模型服务超时',
    'LLM_RESPONSE_TIMEOUT': '等待模型响应超时',
    'LLM_CONNECTION_ERROR': '模型服务连接中断',
    'LLM_DEADLINE_EXCEEDED': '模型请求总时间预算耗尽',
    'LLM_QUEUE_TIMEOUT': '等待模型请求执行名额超时',
    'LLM_QUEUE_FULL': '模型请求队列已满',
    'LLM_CIRCUIT_OPEN': '模型服务暂时处于熔断保护中',
    'LLM_HTTP_ERROR': '模型服务返回 HTTP 错误',
    'LLM_OUTPUT_TRUNCATED': '模型返回被截断，内容不完整',
    'LLM_CONTENT_FILTERED': '模型服务未返回可用内容',
    'LLM_REQUEST_FAILED': '模型请求失败，未记录具体传输阶段',
    'TOOL_UNKNOWN': '未知工具',
    'PAPER_INDEX_NOT_FOUND': '未匹配到待删除的论文索引',
    'TOOL_ACCESS_DENIED': '当前角色无权调用此工具（not available）',
    'TOOL_SOURCE_SCOPE_DENIED': '来源超出了用户指定的研究范围',
    'TOOL_INVALID_ARGUMENT': '工具参数不合法或缺少必要参数',
    'TOOL_SOURCE_NOT_FOUND': '图片不存在或无法在允许目录中访问',
    'TOOL_SOURCE_RESOLUTION_FAILED': '无法从指定论文与图号或页码定位图片',
    'TOOL_UNSUPPORTED_FORMAT': '图片格式不受支持，仅支持 PNG、JPG 或 JPEG',
    'TOOL_INPUT_TOO_LARGE': '图片过大，超过 10MB 上限',
    'TOOL_NOT_CONFIGURED': 'DeepSeek 视觉模型未启用，请先配置模型密钥',
    'TOOL_SOURCE_READ_FAILED': '读取本地来源文件失败',
    'TOOL_RESPONSE_INVALID': '模型响应为空或格式不合法，未取得可用识图结果',
    'TOOL_RESULT_READ_FAILED': '无法读取已保存的工具结果，请核对结果 ID、范围和访问权限',
    'TOOL_RESULT_STORAGE_FAILED': '工具已执行，但完整结果保存失败，无法续读',
    'TOOL_EXECUTION_FAILED': '工具执行异常，具体原因未记录',
    'TOOL_REPORTED_FAILURE': '工具报告失败，但未提供结构化诊断',
}


def error_reason(details: dict) -> str:
    return REASONS.get(details.get('code'), '工具调用失败，原因尚未明确')


def _legacy_reason(text: str) -> str:
    # Legacy tools may interpolate transport exceptions. Do not copy URLs,
    # authorization headers or image payloads into the structured failure.
    text = re.sub(r'data:[^\s]+', '[图片数据已省略]', text, flags=re.I)
    text = re.sub(r'https?://[^\s]+', '[服务地址已省略]', text, flags=re.I)
    text = re.sub(r'(?i)(bearer\s+|sk-)[\w.\-]+', '[凭据已省略]', text)
    text = re.sub(r'''(?i)(["']?(?:api[_-]?key|authorization|password|token)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)''', r'\1[已省略]', text)
    return ' '.join(text.lstrip('❌ ').split())[:240]


class ToolFailure(str):
    """Still a string for existing tools; carries safe diagnostics until archived."""
    def __new__(cls, *, code: str, phase: str = 'unknown', retryable: bool | None = None,
                evidence_status: str = 'unavailable', details: dict | None = None, legacy_reason: str = ''):
        diagnostics = safe_error_details({**(details or {}), 'code': code, 'phase': phase,
                                          'retryable': retryable, 'evidence_status': evidence_status})
        meta = {'version': 1, **diagnostics, 'retryable': diagnostics.get('retryable')}
        # Request IDs remain in diagnostic events, not in model evidence.
        meta.pop('request_id', None)
        reason = error_reason(diagnostics)
        if legacy_reason:
            reason += '。原工具提示：' + _legacy_reason(legacy_reason)
        action = ('可在剩余预算内重试；重试前确认没有重复写入或删除的风险。' if retryable is True else
                  '先修正参数、权限或配置，不要原样重复调用。' if retryable is False else
                  '重试性未知；先核对原因及已执行的副作用，不要盲目重复。')
        if code in {'TOOL_SOURCE_NOT_FOUND', 'TOOL_SOURCE_RESOLUTION_FAILED'}:
            action = '先用 list_paper_images 核对论文、图号或页码，再修正定位参数；不要猜图片路径。'
        if code == 'PAPER_INDEX_NOT_FOUND':
            action = '无需重复删除。重新入库请用 list_papers 的原文路径调用 read_pdf；补译已有论文可直接启动翻译。'
        text = ERROR_HEADER + json.dumps(meta, ensure_ascii=False, separators=(',', ':')) + '\n' + reason + '。' + action
        text += '\n这是工具状态，不是论文证据；不能据此断言原文不存在，也不能宣称已看图核实。'
        obj = super().__new__(cls, text)
        obj.error_details = diagnostics
        return obj


def failure_details(text: str) -> dict:
    first = str(text).split('\n', 1)[0]
    if not first.startswith(ERROR_HEADER):
        return {}
    try:
        meta = json.loads(first[len(ERROR_HEADER):])
        return safe_error_details(meta) if isinstance(meta, dict) and meta.get('version') == 1 else {}
    except (TypeError, ValueError):
        return {}


def failure_from_exception(exc: Exception, *, phase: str = 'execute', code: str = 'TOOL_EXECUTION_FAILED') -> ToolFailure:
    details = safe_error_details(getattr(exc, 'error_details', None))
    details = safe_error_details({'exception_type': type(exc).__name__, **details})
    code = details.get('code', code)
    phase = details.get('phase', phase)
    retryable = None
    if code in {'LLM_CONNECT_TIMEOUT', 'LLM_RESPONSE_TIMEOUT', 'LLM_CONNECTION_ERROR',
                'LLM_QUEUE_TIMEOUT', 'LLM_QUEUE_FULL', 'LLM_CIRCUIT_OPEN', 'LLM_DEADLINE_EXCEEDED'}:
        retryable = True
    elif code == 'LLM_HTTP_ERROR':
        status = details.get('http_status')
        if status is not None:
            retryable = status in {408, 429} or status >= 500
    return ToolFailure(code=code, phase=phase, retryable=retryable, details=details,
                       evidence_status='unavailable' if code.startswith('LLM_') else 'unknown')
