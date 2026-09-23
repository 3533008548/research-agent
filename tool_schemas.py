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
                "name": "save_research_document",
                "description": "创建一份独立研究档案：同时写入可检索 Markdown 和可下载 Word 文档，并自动采用四个固定章节。仅在用户明确要求保存、导出研究方案时调用；已有档案不能整篇覆盖。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "简洁、稳定的文档标题"},
                        "content": {"type": "string", "description": "完整 Markdown 正文；应覆盖研究背景与研究现状、研究内容与创新、研究方案与可行性、研究展望与计划。未分节内容会保留在第一章节。"},
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
                "description": "读取一份研究档案的完整内容，用于用户明确要求查看或全局只读分析。禁止把读取结果作为整篇覆盖写回的依据；修改必须使用章节读取和局部补丁工具。",
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
                "name": "list_research_document_sections",
                "description": "列出研究档案的四个固定章节及摘要。用户要修改档案时必须先调用它，再只读取需要修改的章节。",
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
                "name": "read_research_document_section",
                "description": "读取一个研究档案章节，返回正文、当前 revision 和内容哈希。局部补丁必须原样携带这些并发校验值。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "document_id": {"type": "string", "description": "研究档案 ID"},
                        "section_id": {"type": "string", "enum": ["background", "content_innovation", "approach_feasibility", "outlook_plan"], "description": "要读取的固定章节 ID"},
                    },
                    "required": ["document_id", "section_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "apply_research_document_patch",
                "description": "安全地局部修改已读取的研究档案章节。只在用户明确确认修改内容后调用；必须使用刚读取得到的 base_revision 和 expected_hash。禁止整篇重写，也不得根据截断内容推测未读正文。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "document_id": {"type": "string", "description": "研究档案 ID"},
                        "base_revision": {"type": "integer", "description": "读取章节时返回的当前 revision"},
                        "operations": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 4,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "op": {"type": "string", "enum": ["replace_section", "append_to_section"]},
                                    "section_id": {"type": "string", "enum": ["background", "content_innovation", "approach_feasibility", "outlook_plan"]},
                                    "expected_hash": {"type": "string", "description": "读取该章节时返回的 content_hash"},
                                    "content": {"type": "string", "description": "该章节的新正文或追加正文；不得包含四个固定章节标题"},
                                },
                                "required": ["op", "section_id", "expected_hash", "content"],
                            },
                        },
                    },
                    "required": ["document_id", "base_revision", "operations"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "prepare_research_document_patch_context",
                "description": "仅在用户已明确确认“将全部/所有修改写入”时调用：一次完整读取四个固定章节及 revision/hash，供下一步用一条原子补丁提交。正文过长时会明确拒绝，不会截断后写入。",
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
                "name": "list_research_document_versions",
                "description": "列出研究档案的当前版本和历史快照，不读取正文。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "document_id": {"type": "string", "description": "研究档案 ID"},
                        "limit": {"type": "integer", "description": "返回版本数（1-10，默认 10）"},
                    },
                    "required": ["document_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "restore_research_document_version",
                "description": "将一个历史快照恢复为新版本。仅在用户明确确认要恢复的版本号后调用；恢复前的当前内容也会保留为快照。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "document_id": {"type": "string", "description": "研究档案 ID"},
                        "base_revision": {"type": "integer", "description": "当前版本号"},
                        "revision": {"type": "integer", "description": "要恢复的历史版本号"},
                    },
                    "required": ["document_id", "base_revision", "revision"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "compare_papers_to_research_document",
                "description": "只读地将一至五篇已索引论文和科研档案的全部四个章节做全覆盖证据比对。用于识别可能重叠、可借鉴、差异和不确定项；绝不修改档案，且词汇未命中不代表没有概念关联。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "document_id": {"type": "string", "description": "研究档案 ID"},
                        "paper_ids_or_titles": {"type": "array", "minItems": 1, "maxItems": 5, "items": {"type": "string"}, "description": "已索引论文的 paper_id 或精确标题"},
                    },
                    "required": ["document_id", "paper_ids_or_titles"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_research_document_ledger",
                "description": "读取科研档案的证据与假设账本：其中的主张关联固定章节、可定位论文证据、认识状态和可证伪条件。它不读取或修改全文正文。",
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
                "name": "apply_research_document_ledger_patch",
                "description": "更新科研档案的证据与假设账本，不改正文。仅当用户明确确认要记录这些主张/证据后调用。supported 状态必须有可定位论文证据；每个新增或更新项必须带刚读取章节的哈希。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "document_id": {"type": "string", "description": "研究档案 ID"},
                        "base_revision": {"type": "integer", "description": "读取账本时返回的 ledger_revision"},
                        "operations": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 20,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "op": {"type": "string", "enum": ["upsert_item", "delete_item"]},
                                    "item_id": {"type": "string", "description": "已有项更新或删除时必填；新建时省略"},
                                    "section_id": {"type": "string", "enum": ["background", "content_innovation", "approach_feasibility", "outlook_plan"]},
                                    "expected_section_hash": {"type": "string", "description": "关联章节刚读取时返回的 content_hash"},
                                    "kind": {"type": "string", "enum": ["research_question", "hypothesis", "innovation_candidate", "decision"]},
                                    "status": {"type": "string", "enum": ["open", "hypothesis", "supported", "contested", "rejected", "decision"]},
                                    "statement": {"type": "string", "description": "简洁、可审阅的研究判断"},
                                    "falsification": {"type": "string", "description": "何种观察或实验会否定该判断；可选"},
                                    "evidence": {
                                        "type": "array",
                                        "maxItems": 12,
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "paper_id": {"type": "string"},
                                                "title": {"type": "string"},
                                                "page": {"type": "integer"},
                                                "chunk_index": {"type": "integer"},
                                                "relation": {"type": "string", "enum": ["supports", "contradicts", "conditions", "inspiration"]},
                                                "note": {"type": "string", "description": "该证据支持、冲突或限制判断的简短说明"},
                                            },
                                            "required": ["relation"],
                                        },
                                    },
                                },
                                "required": ["op"],
                            },
                        },
                    },
                    "required": ["document_id", "base_revision", "operations"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "review_research_document_innovation",
                "description": "对一至五篇已索引论文和一份科研档案做只读创新性审查。工具按研究问题、假设、机制、条件、评价五维提供全量词汇扫描和可用的混合检索证据候选；不得把差异直接表述为已证实创新，也不得修改档案或账本。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "document_id": {"type": "string", "description": "研究档案 ID"},
                        "paper_ids_or_titles": {"type": "array", "minItems": 1, "maxItems": 5, "items": {"type": "string"}, "description": "已索引论文的 paper_id 或精确标题"},
                    },
                    "required": ["document_id", "paper_ids_or_titles"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "review_new_paper_impact_on_research_document",
                "description": "针对一篇已唯一定位的新导入论文，生成科研档案综合影响的只读证据包：研究内容重叠/可借鉴、研究方案与可行性、五维创新性候选。一次调用会复用已读取的档案章节和论文切块；只用于提出逐章节修改建议，绝不修改档案、账本或版本。论文不明确时必须先要求用户指定标题或 paper_id。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "document_id": {"type": "string", "description": "研究档案 ID"},
                        "paper_id_or_title": {"type": "string", "description": "这一篇新导入且已索引论文的精确标题或 paper_id；不得传“最新论文”或模糊描述"},
                    },
                    "required": ["document_id", "paper_id_or_title"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "update_experiment_project",
                "description": "将用户明确确认的论文复现实验前提写入独立实验项目，同时更新规格、配置和 README。仅在用户明确确认某个 experiment- 项目的待确认项时调用；不得编造论文参数、下载数据或声称已完成训练。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string", "description": "实验项目 ID，格式 experiment-…"},
                        "confirmed_items": {"type": "array", "items": {"type": "string"}, "description": "用户明确确认的事实或决定；每项简洁、可执行"},
                        "resolved_unknowns": {"type": "array", "items": {"type": "string"}, "description": "被这些确认项解决的原待确认项，必须与项目中的原文完全一致"},
                        "summary": {"type": "string", "description": "这次写入的简短说明"},
                    },
                    "required": ["project_id", "confirmed_items"],
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
