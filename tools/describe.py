"""🖼 ``describe_image`` — DeepSeek 视觉描述（带缓存和图注）。"""

import base64
import json
from pathlib import Path

from integrations.llm_client import RequestPolicy, RequestPriority
from infra.runtime_paths import get_runtime_paths
from infra.cancellation import RequestCancelledError, raise_if_cancelled
from tools.errors import ToolFailure, failure_from_exception


MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_VISION_MODEL = "deepseek-flash"


def handle_describe_image(
    args: dict,
    *,
    memory_store=None,
    paper_store=None,
    llm_client=None,
    vision_model: str = DEFAULT_VISION_MODEL,
    cancel_event=None,
    **_kwargs,
) -> str:
    """Describe a local paper image through the shared DeepSeek client.

    Image analysis is an optional, low-priority request.  It shares the normal
    client for admission control, but failures do not contribute to the chat
    circuit breaker.
    """
    paths = get_runtime_paths()
    image_path = str(args.get("image_path", "") or "").strip().strip('"\'')
    source_note = ""
    if args.get('paper_id_or_title'):
        if image_path:
            return ToolFailure(code='TOOL_INVALID_ARGUMENT', phase='validate', retryable=False)
        try:
            from papers.images import resolve_paper_image
            resolved, source_note = resolve_paper_image(args, paper_store, paths)
            image_path = str(resolved)
        except (ValueError, OSError, TypeError) as exc:
            return ToolFailure(code='TOOL_SOURCE_RESOLUTION_FAILED', phase='resolve_source', retryable=False, legacy_reason=str(exc))
    if not image_path:
        return ToolFailure(code='TOOL_INVALID_ARGUMENT', phase='validate', retryable=False)
    requested = Path(image_path).expanduser()
    p = None
    allowed_dirs = (paths.images_dir, paths.papers_dir)
    if requested.is_absolute() and requested.exists():
        resolved = requested.resolve()
        for directory in allowed_dirs:
            try:
                resolved.relative_to(directory.resolve())
                p = resolved
                break
            except ValueError:
                continue
    else:
        for directory in allowed_dirs:
            candidate = paths.safe_child(directory, requested.name)
            if candidate.exists():
                p = candidate
                break

    if p is None or not p.is_file():
        return ToolFailure(code='TOOL_SOURCE_NOT_FOUND', phase='resolve_source', retryable=False)
    if not source_note:
        from papers.images import asset_source_note
        source_note = asset_source_note(p)
    if p.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        return ToolFailure(code='TOOL_UNSUPPORTED_FORMAT', phase='validate', retryable=False)
    if p.stat().st_size > MAX_IMAGE_BYTES:
        return ToolFailure(code='TOOL_INPUT_TOO_LARGE', phase='validate', retryable=False)
    if llm_client is None:
        return ToolFailure(code='TOOL_NOT_CONFIGURED', phase='validate', retryable=False)
    vision_model = vision_model or DEFAULT_VISION_MODEL
    task = str(args.get("task") or "describe")
    if task not in {"describe", "transcribe_algorithm"}:
        return ToolFailure(code='TOOL_INVALID_ARGUMENT', phase='validate', retryable=False)
    import hashlib
    scope_key = hashlib.sha256(source_note.encode()).hexdigest()[:12] if source_note else ''
    cache_key = (str(p) if task == "describe" else str(p) + "#algorithm-transcription-v1") + scope_key

    # ── 描述缓存命中 ──
    if memory_store:
        cached = memory_store.get_image_description(cache_key)
        if cached:
            return f"🖼️ 图片描述 ({p.name}) [缓存]:\n" + (source_note + '\n' if source_note else '') + cached

    # ── 附带图注（sidecar）──
    caption_text = ""
    try:
        cap_file = p.parent / (p.stem.split("_Figure")[0] + "_captions.json")
        if cap_file.exists():
            caps = json.loads(cap_file.read_text(encoding="utf-8"))
            for label, text in caps.items():
                if label in p.name:
                    caption_text = text
                    break
    except Exception:
        pass

    try:
        img_data = p.read_bytes()
        b64 = base64.b64encode(img_data).decode()
        ext = p.suffix.lstrip(".").lower()
        mime = f"image/{'jpeg' if ext in ('jpg','jpeg') else ext}"
    except OSError as exc:
        return failure_from_exception(exc, code='TOOL_SOURCE_READ_FAILED', phase='read_source')

    prompt = "请详细描述这张图的内容。"
    if task == "transcribe_algorithm":
        prompt = (
            "请逐行转写图片中的算法伪代码，而不是概述算法。保留全部行号、缩进、函数签名、"
            "变量名、上下标、集合操作、比较符号和循环边界，直到最后一行及辅助函数。"
            "特别核对 <= 与 <、!= 与 =、入队对象和下标。不能从算法常识推断或补写缺失行。"
            "无法辨认的位置用[无法辨认]标记；有歧义时列出原行号及可能字符。"
            "先输出保留原文的代码块，再用中文列出无法核验的符号。"
        )
    elif caption_text:
        prompt += f"\n论文中的图注是: 「{caption_text[:200]}」\n请结合图注理解图片，描述各子图的内容和它们的关系。"
    else:
        prompt += "如果是网络架构图，描述每层结构和数据流；如果是流程图，描述每个步骤；如果是实验数据图，描述数据和结论。用中文回答。"
    if source_note:
        prompt += '\n来源定位：' + source_note

    try:
        resp = llm_client.post(
            {
                "model": vision_model,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ]}],
                "temperature": 0.3,
                "stream": False,
            },
            policy=RequestPolicy(
                purpose="describe_image",
                priority=RequestPriority.SUMMARY,
                deadline_seconds=60,
                max_retries=0,
                counts_toward_circuit=False,
            ),
            cancel_event=cancel_event,
        )
        try:
            choice = resp.json()["choices"][0]
            desc = choice["message"]["content"]
            if choice.get('finish_reason') in {'length', 'content_filter'}:
                return ToolFailure(code='LLM_OUTPUT_TRUNCATED' if choice['finish_reason'] == 'length' else 'LLM_CONTENT_FILTERED',
                                   phase='parse_response', evidence_status='partial' if desc else 'unavailable')
            if not isinstance(desc, str) or not desc.strip():
                return ToolFailure(code='TOOL_RESPONSE_INVALID', phase='parse_response')
        except (ValueError, KeyError, IndexError, TypeError):
            return ToolFailure(code='TOOL_RESPONSE_INVALID', phase='parse_response')
        finally:
            resp.close()
    except RequestCancelledError:
        raise
    except Exception as exc:
        raise_if_cancelled(cancel_event, '识图已取消')
        return failure_from_exception(exc, phase='unknown', code='LLM_REQUEST_FAILED')

    # ── 存入缓存 ──
    if memory_store:
        try:
            memory_store.save_image_description(cache_key, desc)
        except Exception:
            pass

    return f"🖼️ 图片描述 ({p.name}):\n" + (source_note + '\n' if source_note else '') + desc
