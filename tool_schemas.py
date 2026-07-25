"""
📋 工具 Schema 定义 — Function Calling schema 列表
"""


def get_tool_schemas() -> list[dict]:
    """返回所有工具的 JSON Schema 定义"""
    return [
        {
            "type": "function",
            "function": {
                "name": "search_papers",
                "description": "搜索学术论文。semantic_scholar（含引用数+PDF）和 arxiv 双源。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词"},
                        "source": {"type": "string", "enum": ["semantic_scholar", "arxiv"], "description": "数据源"},
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
                "description": "在已索引论文中语义检索方法细节/公式/实验数据。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "查询内容"},
                        "top_k": {"type": "integer", "description": "返回段落数"},
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
                "description": "搜索记忆库中的论文关系三元组（论文→方法/结果）和对话摘要。",
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
