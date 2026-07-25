"""
⚙️ 统一配置 — 合并 .env + config.yaml + CLI 参数

优先级: CLI > 环境变量 > config.yaml > 默认值

用法:
  cfg = Config.load()
  cfg.model          → "deepseek-v4-flash"
  cfg.deepseek_key   → "sk-xxx"
  cfg.pdf_max_pages  → 15
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

# ── 默认值 ──
_DFLT = {
    "model": "deepseek-v4-flash",
    "pdf_max_pages": 15,
    "rag_enabled": True,
    "ui_port": 7860,
    "ui_debug": False,
    "checkpoint_db": "checkpoint.db",
    "chroma_dir": "chroma_data",
    "papers_dir": "data/papers",
}


@dataclass
class Config:
    model: str = "deepseek-chat"
    deepseek_key: str = ""
    glm_key: str = ""
    hf_endpoint: str = ""

    pdf_max_pages: int = 15
    rag_enabled: bool = True

    ui_port: int = 7860
    ui_debug: bool = False

    checkpoint_db: str = "checkpoint.db"
    chroma_dir: str = "chroma_data"
    papers_dir: str = "data/papers"

    @classmethod
    def load(cls, cli_overrides: dict | None = None) -> "Config":
        """加载配置：.env → config.yaml → CLI overrides"""
        # ── 1. 基础默认 ──
        cfg = {k: v for k, v in _DFLT.items()}

        # ── 2. .env 加载（API Key 等） ──
        try:
            from dotenv import load_dotenv as _load_dotenv
            for p in [".env", str(Path.home() / ".env")]:
                if os.path.exists(p):
                    _load_dotenv(p)
                    break
            _load_dotenv()
        except ImportError:
            pass

        # ── 3. config.yaml 加载 ──
        try:
            import yaml
            if Path("config.yaml").exists():
                with open("config.yaml", encoding="utf-8") as f:
                    yaml_cfg = yaml.safe_load(f) or {}
                if "model" in yaml_cfg:
                    cfg["model"] = yaml_cfg["model"]
                if "pdf" in yaml_cfg:
                    cfg["pdf_max_pages"] = yaml_cfg["pdf"].get("max_pages", cfg["pdf_max_pages"])
                if "rag" in yaml_cfg:
                    cfg["rag_enabled"] = yaml_cfg["rag"].get("enabled", cfg["rag_enabled"])
                if "ui" in yaml_cfg:
                    cfg["ui_port"] = yaml_cfg["ui"].get("port", cfg["ui_port"])
        except Exception:
            pass

        # ── 4. 环境变量覆盖 ──
        env_map = {
            "DEEPSEEK_API_KEY": "deepseek_key",
            "GLM_API_KEY": "glm_key",
            "HF_ENDPOINT": "hf_endpoint",
            "DEEPSEEK_MODEL": "model",
        }
        for env, attr in env_map.items():
            val = os.getenv(env, "")
            if val:
                cfg[attr] = val

        # ── 5. CLI 覆盖 ──
        if cli_overrides:
            cfg.update({k: v for k, v in cli_overrides.items() if v is not None})

        return cls(
            model=cfg["model"],
            deepseek_key=cfg.get("deepseek_key", "") or os.getenv("DEEPSEEK_API_KEY", ""),
            glm_key=cfg.get("glm_key", "") or os.getenv("GLM_API_KEY", ""),
            hf_endpoint=cfg.get("hf_endpoint", ""),
            pdf_max_pages=cfg["pdf_max_pages"],
            rag_enabled=cfg["rag_enabled"],
            ui_port=cfg["ui_port"],
            ui_debug=cfg.get("ui_debug", False),
            checkpoint_db=cfg.get("checkpoint_db", "checkpoint.db"),
            chroma_dir=cfg.get("chroma_dir", "chroma_data"),
            papers_dir=cfg.get("papers_dir", "data/papers"),
        )
