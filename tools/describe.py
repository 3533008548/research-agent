"""
🖼 describe_image 工具 — GLM-4V 看图描述（带缓存 + 图注）
"""

import base64
import json
import requests
import sys
from pathlib import Path
from runtime_paths import get_runtime_paths


def handle_describe_image(args: dict, glm_api_key: str = "", memory_store=None, **kw) -> str:
    image_path = args.get("image_path", "")
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

    if p is None:
        return f"❌ 图片不存在: {image_path}"
    if not glm_api_key:
        return "❌ GLM-4V API Key 未配置。请在 .env 中设置 GLM_API_KEY。"

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
        resp = requests.post(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            headers={"Authorization": f"Bearer {glm_api_key}", "Content-Type": "application/json"},
            json={
                "model": "glm-4v",
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ]}],
                "temperature": 0.3, "stream": False,
            },
            timeout=60,
        )
        resp.raise_for_status()
        desc = resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"❌ GLM-4V 调用失败: {e}"

    # ── 存入缓存 ──
    if memory_store:
        try:
            memory_store.save_image_description(str(p), desc)
        except Exception:
            pass

    return f"🖼️ 图片描述 ({p.name}):\n{desc}"
