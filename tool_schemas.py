"""
📋 工具 Schema 定义 — Function Calling schema 列表
"""


def _raw_tool_schemas() -> list[dict]:
    """Return the static schemas used to construct the public tool catalog."""
    return [
        {
            "type": "function", "function": {
                "name": "list_tool_results",
                "description": "查找当前会话已保存的完整工具返回。上下文压缩、丢失结果 ID 或重启后用此目录恢复入口；query 会搜索工具原文。只访问当前允许使用的工具结果。",
                "parameters": {"type": "object", "properties": {
                    "query": {"type": "string", "description": "可选：原文关键词或工具名"},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10}
                }}
            }
        },
        {
            "type": "function", "function": {
                "name": "read_tool_result",
                "description": "按 result_id 续读保存的工具原文。offset 是原文字符位置，范围左闭右开；按 next_offset 继续。工具结果完整不等于源 PDF/OCR 完整。",
                "parameters": {"type": "object", "properties": {
                    "result_id": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 8000}
                }, "required": ["result_id"]}
            }
        },
        {
            "type": "function", "function": {
                "name": "search_tool_result",
                "description": "在指定结果的完整保存原文中搜索字面关键词，返回精确字符位置及附近片段。核对遗漏的算法末行、图号或结论时使用；未命中不能证明源 PDF 中不存在。",
                "parameters": {"type": "object", "properties": {
                    "result_id": {"type": "string"}, "query": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 5}
                }, "required": ["result_id", "query"]}
            }
        },
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
                        "url_or_path": {"type": "string", "description": "PDF URL，或 list_papers 返回的相对路径（含 library/ 子目录）。桌面客户端也支持用户提供的本机 PDF 绝对路径，会先自动导入论文库；网页服务需先上传。不要猜测数据目录。"},
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
                "description": "查看论文原图。可提供 image_path，或直接提供 paper_id_or_title 与 figure/page，自动从原 PDF 生成图片，不必让用户截图或提供路径。算法核对用 transcribe_algorithm。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "image_path": {"type": "string", "description": "图片路径"},
                        "paper_id_or_title": {"type": "string", "description": "论文标题或 paper_id；与 figure 或 page 合用，和 image_path 二选一"},
                        "figure": {"type": "string", "description": "图号，例如 4、Fig. 4 或图四；自动定位原页，包括矢量图"},
                        "page": {"type": "integer", "minimum": 1, "maximum": 100, "description": "从 1 开始的 PDF 页码；可与图号合用消除歧义"},
                        "task": {"type": "string", "enum": ["describe", "transcribe_algorithm"], "description": "默认 describe；算法原文核对用 transcribe_algorithm，保留行号、缩进和符号"},
                    },
                    "anyOf": [{"required": ["image_path"]}, {"required": ["paper_id_or_title", "figure"]}, {"required": ["paper_id_or_title", "page"]}],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_paper_images",
                "description": "查找论文图片的图号、页码和实际保存路径。兼容旧索引，缺失图片会从原 PDF 恢复；矢量图提供包含该图的整页原图。历史读取结果被截断时用此工具重新查找。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "paper_id_or_title": {"type": "string", "description": "已索引论文标题或 paper_id"},
                        "figure": {"type": "string", "description": "可选图号，例如 4、Fig. 4 或图四"},
                        "page": {"type": "integer", "minimum": 1, "maximum": 100},
                        "offset": {"type": "integer", "minimum": 0, "description": "目录分页起点，默认 0"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 10, "description": "最多返回条数，默认 10"},
                    },
                    "required": ["paper_id_or_title"],
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
                    "指定算法编号（Algorithm 1/算法一）时自动从原 PDF 读取完整算法框并提供原页裁图，兼容旧索引；"
                    "开放式方法/主题提问（未指定论文）不要传该参数，以便借助论文关系召回相关论文。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "查询内容"},
                        "top_k": {"type": "integer", "description": "普通检索返回段落数；明确算法编号时返回完整算法区域"},
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
