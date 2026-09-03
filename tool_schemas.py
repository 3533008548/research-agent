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
                "description": "搜索学术论文。默认合并 OpenAlex 与 arXiv、去重并保留来源；也可限定单一来源。",
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
                "description": "下载论文PDF并提取文本。自动索引+提取图片。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url_or_path": {"type": "string", "description": "PDF URL 或路径"},
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
                "name": "save_research_document",
                "description": "保存或更新一份独立研究档案：同时写入可检索 Markdown 和可下载 Word 文档。仅在用户明确要求保存、导出或更新研究方案时调用；不要改写用户画像或会话摘要。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "简洁、稳定的文档标题"},
                        "content": {"type": "string", "description": "完整 Markdown 正文；应自包含研究问题、假设、方法、计划和待验证项等用户要求的内容"},
                        "document_id": {"type": "string", "description": "仅更新已有档案时传入；先搜索获取 ID"},
                    },
                    "required": ["title", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_research_documents",
                "description": "按需搜索用户已保存的研究档案；用于查找此前方案、假设、实验计划或决策记录。不会把所有档案自动放入上下文。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "关键词；为空时列出最近更新档案"},
                        "limit": {"type": "integer", "description": "返回数量（1-10，默认 5）"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_research_document",
                "description": "读取一份研究档案的完整内容。仅在已从搜索结果获得明确文档 ID，或用户明确给出 ID 时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "document_id": {"type": "string", "description": "研究档案 ID"},
                    },
                    "required": ["document_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "describe_image",
                "description": "用 GLM-4V 描述论文图片（架构图/流程图/实验图）。",
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
                "name": "query_papers",
                "description": "在已索引论文中语义检索方法细节/公式/实验数据。支持按章节过滤。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "查询内容"},
                        "top_k": {"type": "integer", "description": "返回段落数"},
                        "section": {"type": "string", "description": "限定章节（如 Method/Experiments），可选"},
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
                "description": "更新用户画像（研究方向/偏好/活跃问题/已读论文）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["add_direction", "add_question", "add_paper", "set_preference"],
                        },
                        "content": {"type": "string", "description": "内容文本"},
                    },
                    "required": ["action", "content"],
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
