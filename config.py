"""
⚙️ 统一配置 — 合并 .env + config.yaml + CLI 参数

优先级: CLI > 环境变量 > runtime/settings.json > config.yaml > 默认值

用法:
  cfg = Config.load()
  cfg.model          → "deepseek-v4-flash"
  cfg.deepseek_key   → "sk-xxx"
  cfg.pdf_max_pages  → 15
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from runtime_paths import RuntimePaths

# ── 默认值 ──
_DFLT = {
    "model": "deepseek-v4-flash",
    "pdf_max_pages": 15,
    "rag_enabled": True,
    "rag_reranker_enabled": False,
    "rag_reranker_model": "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
    "rag_reranker_candidate_limit": 20,
    "ui_port": 7860,
    "ui_debug": False,
    "verify_timeout_seconds": 8,
    "daily_request_timeout_seconds": 8,
    "daily_keyword_concurrency": 2,
    "daily_max_results_per_keyword": 3,
    "daily_sources": ("openalex", "openaire", "dblp"),
    "api_max_concurrency": 4,
    "api_interactive_reserved_slots": 1,
    "api_queue_size": 20,
    "api_connect_timeout_seconds": 3.05,
    "api_read_timeout_seconds": 30,
    "api_request_deadline_seconds": 45,
    "api_max_retries": 2,
    "api_circuit_failure_threshold": 3,
    "api_circuit_recovery_seconds": 120,
    "api_url": "https://api.deepseek.com/chat/completions",
}


@dataclass
class Config:
    model: str = "deepseek-chat"
    deepseek_key: str = ""
    api_url: str = "https://api.deepseek.com/chat/completions"
    glm_key: str = ""
    hf_endpoint: str = ""
    openalex_api_key: str = ""

    pdf_max_pages: int = 15
    rag_enabled: bool = True
    rag_reranker_enabled: bool = False
    rag_reranker_model: str = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
    rag_reranker_candidate_limit: int = 20

    ui_port: int = 7860
    ui_debug: bool = False
    daily_search_enabled: bool = False
    verify_timeout_seconds: int = 8
    daily_request_timeout_seconds: int = 8
    daily_keyword_concurrency: int = 2
    daily_max_results_per_keyword: int = 3
    daily_sources: tuple[str, ...] = ("openalex", "openaire", "dblp")
    api_max_concurrency: int = 4
    api_interactive_reserved_slots: int = 1
    api_queue_size: int = 20
    api_connect_timeout_seconds: float = 3.05
    api_read_timeout_seconds: int = 30
    api_request_deadline_seconds: int = 45
    api_max_retries: int = 2
    api_circuit_failure_threshold: int = 3
    api_circuit_recovery_seconds: int = 120

    data_dir: str = "runtime"
    checkpoint_db: str = ""
    memory_db: str = ""
    daily_db: str = ""
    chroma_dir: str = ""
    papers_dir: str = ""
    images_dir: str = ""
    profile_path: str = ""
    runtime_paths: RuntimePaths = field(init=False, repr=False)

    def __post_init__(self) -> None:
        paths = RuntimePaths.from_root(self.data_dir)
        paths.ensure_initialized()
        self.runtime_paths = paths
        self.data_dir = str(paths.root)
        # 工具模块无需持有 Config；同步到进程环境确保它们使用同一数据根目录。
        os.environ["APP_DATA_DIR"] = self.data_dir
        self.checkpoint_db = self.checkpoint_db or str(paths.checkpoint_db)
        self.memory_db = self.memory_db or str(paths.memory_db)
        self.daily_db = self.daily_db or str(paths.daily_db)
        self.chroma_dir = self.chroma_dir or str(paths.chroma_dir)
        self.papers_dir = self.papers_dir or str(paths.papers_dir)
        self.images_dir = self.images_dir or str(paths.images_dir)
        self.profile_path = self.profile_path or str(paths.profile_path)

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
        config_path = Path("config.yaml")
        try:
            import yaml
        except ImportError as exc:
            if config_path.exists():
                raise RuntimeError(
                    "检测到 config.yaml，但缺少 PyYAML；请安装 requirements.txt"
                ) from exc
            yaml = None
        if yaml is not None and config_path.exists():
            try:
                with config_path.open(encoding="utf-8") as f:
                    yaml_cfg = yaml.safe_load(f) or {}
                if not isinstance(yaml_cfg, dict):
                    raise ValueError("config.yaml 的顶层必须是键值对象")
                if "model" in yaml_cfg:
                    cfg["model"] = yaml_cfg["model"]
                if "pdf" in yaml_cfg:
                    cfg["pdf_max_pages"] = yaml_cfg["pdf"].get("max_pages", cfg["pdf_max_pages"])
                if "rag" in yaml_cfg:
                    rag_settings = yaml_cfg["rag"]
                    cfg["rag_enabled"] = rag_settings.get("enabled", cfg["rag_enabled"])
                    reranker = rag_settings.get("reranker", {})
                    if isinstance(reranker, dict):
                        cfg["rag_reranker_enabled"] = reranker.get(
                            "enabled", cfg["rag_reranker_enabled"],
                        )
                        cfg["rag_reranker_model"] = reranker.get(
                            "model", cfg["rag_reranker_model"],
                        )
                        cfg["rag_reranker_candidate_limit"] = reranker.get(
                            "candidate_limit", cfg["rag_reranker_candidate_limit"],
                        )
                if "ui" in yaml_cfg:
                    cfg["ui_port"] = yaml_cfg["ui"].get("port", cfg["ui_port"])
                if "daily_search" in yaml_cfg:
                    cfg["daily_search_enabled"] = yaml_cfg["daily_search"].get("enabled", False)
                    cfg["daily_request_timeout_seconds"] = yaml_cfg["daily_search"].get(
                        "request_timeout_seconds", cfg["daily_request_timeout_seconds"]
                    )
                    cfg["daily_keyword_concurrency"] = yaml_cfg["daily_search"].get(
                        "keyword_concurrency", cfg["daily_keyword_concurrency"]
                    )
                    cfg["daily_max_results_per_keyword"] = yaml_cfg["daily_search"].get(
                        "max_results_per_keyword", cfg["daily_max_results_per_keyword"]
                    )
                    configured_sources = yaml_cfg["daily_search"].get("sources")
                    if isinstance(configured_sources, list):
                        allowed_sources = {"openalex", "openaire", "dblp"}
                        sources = tuple(
                            str(source).strip().lower()
                            for source in configured_sources
                            if str(source).strip().lower() in allowed_sources
                        )
                        if sources:
                            cfg["daily_sources"] = sources
                if "agent" in yaml_cfg:
                    cfg["verify_timeout_seconds"] = yaml_cfg["agent"].get(
                        "verify_timeout_seconds", cfg["verify_timeout_seconds"]
                    )
                if "api_resilience" in yaml_cfg:
                    settings = yaml_cfg["api_resilience"]
                    for key in (
                        "max_concurrency", "interactive_reserved_slots", "queue_size", "connect_timeout_seconds",
                        "read_timeout_seconds", "request_deadline_seconds", "max_retries",
                        "circuit_failure_threshold", "circuit_recovery_seconds",
                    ):
                        config_key = f"api_{key}"
                        if key in settings:
                            cfg[config_key] = settings[key]
            except (OSError, ValueError, yaml.YAMLError, TypeError, AttributeError) as exc:
                raise ValueError(f"无法读取 config.yaml：{exc}") from exc

        # ── 4. 运行时用户设置（UI 修改写入这里，不污染源码配置） ──
        cli_data_dir = (cli_overrides or {}).get("data_dir")
        data_dir = cli_data_dir or os.getenv("APP_DATA_DIR") or "runtime"
        paths = RuntimePaths.from_root(data_dir)
        paths.ensure_initialized()
        user_settings = paths.read_settings()
        validators = {
            "model": lambda value: isinstance(value, str) and bool(value.strip()),
            "rag_enabled": lambda value: isinstance(value, bool),
            "pdf_max_pages": lambda value: isinstance(value, int) and not isinstance(value, bool),
            "daily_search_enabled": lambda value: isinstance(value, bool),
        }
        for key, valid in validators.items():
            if key in user_settings and valid(user_settings[key]):
                cfg[key] = user_settings[key]

        # ── 5. 环境变量覆盖 ──
        env_map = {
            "DEEPSEEK_API_KEY": "deepseek_key",
            "DEEPSEEK_API_URL": "api_url",
            "GLM_API_KEY": "glm_key",
            "HF_ENDPOINT": "hf_endpoint",
            "OPENALEX_API_KEY": "openalex_api_key",
            "DEEPSEEK_MODEL": "model",
        }
        for env, attr in env_map.items():
            val = os.getenv(env, "")
            if val:
                cfg[attr] = val

        # ── 6. CLI 覆盖 ──
        if cli_overrides:
            cfg.update({k: v for k, v in cli_overrides.items() if v is not None})

        return cls(
            model=cfg["model"],
            deepseek_key=cfg.get("deepseek_key", "") or os.getenv("DEEPSEEK_API_KEY", ""),
            api_url=cfg.get("api_url", _DFLT["api_url"]),
            glm_key=cfg.get("glm_key", "") or os.getenv("GLM_API_KEY", ""),
            hf_endpoint=cfg.get("hf_endpoint", ""),
            openalex_api_key=cfg.get("openalex_api_key", "") or os.getenv("OPENALEX_API_KEY", ""),
            pdf_max_pages=cfg["pdf_max_pages"],
            rag_enabled=cfg["rag_enabled"],
            rag_reranker_enabled=bool(cfg.get("rag_reranker_enabled", False)),
            rag_reranker_model=str(cfg.get("rag_reranker_model") or _DFLT["rag_reranker_model"]),
            rag_reranker_candidate_limit=max(
                1, min(20, int(cfg.get("rag_reranker_candidate_limit", 20))),
            ),
            ui_port=cfg["ui_port"],
            ui_debug=cfg.get("ui_debug", False),
            daily_search_enabled=cfg.get("daily_search_enabled", False),
            verify_timeout_seconds=max(3, int(cfg.get("verify_timeout_seconds", 8))),
            daily_request_timeout_seconds=max(3, int(cfg.get("daily_request_timeout_seconds", 8))),
            daily_keyword_concurrency=max(1, min(2, int(cfg.get("daily_keyword_concurrency", 2)))),
            daily_max_results_per_keyword=max(1, min(5, int(cfg.get("daily_max_results_per_keyword", 3)))),
            daily_sources=tuple(cfg.get("daily_sources", _DFLT["daily_sources"])),
            api_max_concurrency=max(1, int(cfg.get("api_max_concurrency", 4))),
            api_interactive_reserved_slots=max(
                0, int(cfg.get("api_interactive_reserved_slots", 1))
            ),
            api_queue_size=max(0, int(cfg.get("api_queue_size", 20))),
            api_connect_timeout_seconds=max(1.0, float(cfg.get("api_connect_timeout_seconds", 3.05))),
            api_read_timeout_seconds=max(3, int(cfg.get("api_read_timeout_seconds", 30))),
            api_request_deadline_seconds=max(5, int(cfg.get("api_request_deadline_seconds", 45))),
            api_max_retries=max(0, int(cfg.get("api_max_retries", 2))),
            api_circuit_failure_threshold=max(1, int(cfg.get("api_circuit_failure_threshold", 3))),
            api_circuit_recovery_seconds=max(10, int(cfg.get("api_circuit_recovery_seconds", 120))),
            data_dir=str(paths.root),
        )
