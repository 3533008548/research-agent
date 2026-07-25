"""
🖼 describe_image 工具 — GLM-4V 看图描述
"""

import base64
import requests
import sys
from pathlib import Path


def handle_describe_image(args: dict, glm_api_key: str = "", **kw) -> str:
    image_path = args.get("image_path", "")
    if not image_path:
        return "❌ 请提供图片路径。"

    p = Path(image_path)
    for d in [Path("."), Path("data/papers"), Path("data/papers/images")]:
        candidate = d / p.name
        if candidate.exists():
            p = candidate; break

    if not p.exists():
        return f"❌ 图片不存在: {image_path}"
    if not glm_api_key:
        return "❌ GLM-4V API Key 未配置。请在 .env 中设置 GLM_API_KEY。"

    try:
        img_data = p.read_bytes()
        b64 = base64.b64encode(img_data).decode()
        ext = p.suffix.lstrip(".").lower()
        mime = f"image/{'jpeg' if ext in ('jpg','jpeg') else ext}"
    except Exception as e:
        return f"❌ 读取图片失败: {e}"

    try:
        resp = requests.post(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            headers={"Authorization": f"Bearer {glm_api_key}", "Content-Type": "application/json"},
            json={
                "model": "glm-4v",
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "请详细描述这张图的内容。如果是网络架构图，描述每层的结构和数据流；如果是流程图，描述每个步骤；如果是实验数据图，描述数据和结论。用中文回答。"},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ]}],
                "temperature": 0.3, "stream": False,
            },
            timeout=60,
        )
        resp.raise_for_status()
        desc = resp.json()["choices"][0]["message"]["content"]
        return f"🖼️ 图片描述 ({p.name}):\n{desc}"
    except Exception as e:
        return f"❌ GLM-4V 调用失败: {e}"
