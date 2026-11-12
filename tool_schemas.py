"""
📋 工具 Schema 定义 — Function Calling schema 列表
"""


def _raw_tool_schemas() -> list[dict]:
    """Return the static schemas used to construct the public tool catalog."""
    return [
        {
            "type": "function",
            "function": {
                "name": "search_papers",
                "description": "搜索学术论文。默认合并 OpenAlex 与 arXiv，去重并保留来源；也可限定单一来源。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词"},
                        "source": {"type": "string", "enum": ["all", "openalex", "arxiv"], "description": "数据源，默认 all"},
                        "limit": {"type": "integer", "description": "返回数（1-10）"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_pdf",
                "description": "下载论文 PDF 并提取文本。自动索引和提取图片；若来自检索结果或用户已给出论文名，应传入 title，避免以 arXiv 编号或缓存文件名命名。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url_or_path": {"type": "string", "description": "PDF URL 或路径"},
                        "title": {"type": "string", "description": "可选的论文正式标题；优先使用已检索到或用户提供的书目信息"},
                        "max_pages": {"type": "integer", "description": "最大页数"},
                    },
                    "required": ["url_or_path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "generate_paper_card",
                "description": "为已索引的本地 PDF 生成可追溯论文证据卡，结论带页码和来源块锚点。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "paper_id_or_title": {"type": "string", "description": "已索引论文的标题或 paper_id"},
                        "max_pages": {"type": "integer", "description": "最多解析页数（1-100，默认 100）"},
                    },
                    "required": ["paper_id_or_title"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "describe_image",
                "description": "用 DeepSeek 视觉模型描述论文图片（架构图/流程图/实验图）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "image_path": {"type": "string", "description": "图片路径"},
                    },
                    "required": ["image_path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_paper_relations",
                "description": "查看用户已确认的论文关系；可限定一篇已索引论文。关系只用于辅助检索候选与重排，不等同论文事实。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "paper_id_or_title": {"type": "string", "description": "可选：已索引论文的精确标题或 paper_id"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_relation_candidates",
                "description": (
                    "查看论文入库时自动审查产生的、尚未确认的论文关系候选。"
                    "候选只是待确认线索，不会参与检索；确认需走 save_paper_relation 并获得用户明确同意。"
                    "用户问「有哪些待确认关系」「自动发现了什么关系」时使用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "paper_id_or_title": {"type": "string", "description": "可选：只看某一篇论文的候选"},
                        "status": {"type": "string", "enum": ["pending", "accepted", "rejected"], "description": "候选状态，默认 pending"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "save_paper_relation",
                "description": "保存用户明确确认的、带页码或片段锚点的论文关系。调用前必须先展示拟写入的方向、类型、说明和证据并获得确认；禁止根据模型猜测自动建关系。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "relation_id": {"type": "string", "description": "更新已有关系时填写；新建时省略"},
                        "source_paper_id_or_title": {"type": "string", "description": "关系起点的已索引论文精确标题或 paper_id"},
                        "target_paper_id_or_title": {"type": "string", "description": "关系终点的已索引论文精确标题或 paper_id"},
                        "relation_type": {"type": "string", "enum": ["method_similar", "method_improves", "experiment_comparable", "result_conflicts", "explicit_citation"], "description": "关系类型；method_improves 和 explicit_citation 保留 source 到 target 的方向"},
                        "note": {"type": "string", "description": "简短、可审阅的关系说明，不得写成未经证实的科学结论"},
                        "confirmed_by_user": {"type": "boolean", "description": "仅在用户已明确确认本次写入时设为 true"},
                        "evidence": {
                            "type": "array", "minItems": 1, "maxItems": 8,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "paper_id": {"type": "string", "description": "两端论文之一的 paper_id 或精确标题"},
                                    "page": {"type": "integer", "description": "可选：证据所在页码"},
                                    "chunk_index": {"type": "integer", "description": "可选：证据所在检索片段序号"},
                                    "note": {"type": "string", "description": "这处证据为何支撑这条关系"},
                                },
                                "required": ["paper_id", "note"],
                            },
                        },
                    },
                    "required": ["source_paper_id_or_title", "target_paper_id_or_title", "relation_type", "note", "confirmed_by_user", "evidence"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delete_paper_relation",
                "description": "删除一条用户明确确认要移除的论文关系；不会删除论文或其正文。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "relation_id": {"type": "string", "description": "要删除的 paper-rel-… ID"},
                        "confirmed_by_user": {"type": "boolean", "description": "仅在用户已明确确认删除时设为 true"},
                    },
                    "required": ["relation_id", "confirmed_by_user"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "query_papers",
                "description": (
                    "在已索引论文中语义检索方法细节/公式/实验数据。支持按章节过滤。"
                    "当用户已指定论文时（如“论文A的…”“比较A和B”），用 paper_id_or_title 限定范围，"
                    "此时不会跨论文扩展，结果只来自指定论文；"
                    "开放式方法/主题提问（未指定论文）不要传该参数，以便借助论文关系召回相关论文。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "查询内容"},
                        "top_k": {"type": "integer", "description": "返回段落数"},
                        "section": {"type": "string", "description": "限定章节（如 Method/Experiments），可选"},
                        "paper_id_or_title": {
                            "description": (
                                "限定检索范围的论文，可传标题/paper_id，或它们的数组（比较多篇时）。"
                                "仅在用户明确指定论文时传；未指定时不要传。"
                            ),
                            "oneOf": [
                                {"type": "string"},
                                {"type": "array", "items": {"type": "string"}},
                            ],
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_papers",
                "description": "列出本地已下载缓存的PDF文件。",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_indexed_papers",
                "description": "查看 RAG 向量库中已索引论文。",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delete_paper",
                "description": "从 RAG 库删除一篇已索引论文。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "paper_id_or_title": {"type": "string", "description": "标题或 paper_id"},
                    },
                    "required": ["paper_id_or_title"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "update_profile",
                "description": "新增或更新用户画像；删除/修正须先 read_profile，按章节和完整原文精确匹配（不含列表前缀）。相同条目去重；无匹配、未知操作报错。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["add_direction", "add_question", "add_paper", "set_preference", "remove_entry", "replace_entry"],
                        },
                        "content": {"type": "string", "description": "单行内容；add_paper 用「标题: 摘要」，set_preference 用「键: 值」。删除/修正传原条目完整文本（不含 - 前缀，仅忽略首尾空白），不支持子串或模糊匹配。"},
                        "section": {
                            "type": "string",
                            "enum": ["研究方向", "偏好设置", "活跃问题", "已读论文"],
                            "description": "remove_entry/replace_entry 必填；其余操作不传。",
                        },
                        "replacement": {"type": "string", "description": "仅 replace_entry 必填：新的完整单行条目；偏好用「键: 值」。同章节的完全相同副本合并处理。"},
                    },
                    "required": ["action", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_profile",
                "description": "读取指定画像章节的条目原文，包含未注入对话摘要的已读论文。删除或修正前用此工具确认完整原文。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "section": {"type": "string", "enum": ["研究方向", "偏好设置", "活跃问题", "已读论文"]},
                    },
                    "required": ["section"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "memory_search",
                "description": "搜索当前会话摘要和全局论文关系三元组（论文→方法/结果）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索词"},
                    },
                    "required": ["query"],
                },
            },
        },
    ]


def get_tool_schemas() -> list[dict]:
    """兼容入口：返回工具目录生成的独立 schema 副本。"""
    from tool_catalog import get_tool_schemas as _get_tool_schemas

    return _get_tool_schemas()
