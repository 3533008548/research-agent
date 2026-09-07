"""🖼 ``describe_image`` — DeepSeek 视觉描述（带缓存和图注）。"""

import base64
import json
from pathlib import Path

from llm_client import RequestPolicy, RequestPriority
from runtime_paths import get_runtime_paths


MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_VISION_MODEL = "deepseek-v4-flash-vision-exp"


def handle_describe_image(
    args: dict,
    *,
    memory_store=None,
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
    image_path = str(args.get("image_path", "") or "").strip()
    if not image_path:
        return "❌ 请提供图片路径。"

    paths = get_runtime_paths()
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
        return f"❌ 图片不存在: {image_path}"
    if p.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        return "❌ 仅支持 PNG、JPG 或 JPEG 图片。"
    if p.stat().st_size > MAX_IMAGE_BYTES:
        return "❌ 图片过大（上限 10MB），请先压缩后再分析。"
    if llm_client is None:
        return "❌ DeepSeek 视觉模型未启用，请先配置 DEEPSEEK_API_KEY。"
    vision_model = vision_model or DEFAULT_VISION_MODEL

    # ── 描述缓存命中 ──
    if memory_store:
        cached = memory_store.get_image_description(str(p))
        if cached:
            return f"🖼️ 图片描述 ({p.name}) [缓存]:\n{cached}"

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
    except Exception as e:
        return f"❌ 读取图片失败: {e}"

    prompt = "请详细描述这张图的内容。"
    if caption_text:
        prompt += f"\n论文中的图注是: 「{caption_text[:200]}」\n请结合图注理解图片，描述各子图的内容和它们的关系。"
    else:
        prompt += "如果是网络架构图，描述每层结构和数据流；如果是流程图，描述每个步骤；如果是实验数据图，描述数据和结论。用中文回答。"

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
            desc = resp.json()["choices"][0]["message"]["content"]
        finally:
            resp.close()
    except Exception as e:
        return f"❌ DeepSeek 视觉模型调用失败: {e}"

    # ── 存入缓存 ──
    if memory_store:
        try:
            memory_store.save_image_description(str(p), desc)
        except Exception:
            pass

    return f"🖼️ 图片描述 ({p.name}):\n{desc}"
