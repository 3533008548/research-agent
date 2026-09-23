"""Deterministic intent hints for a request made from one research dossier.

The router is deliberately narrow: it chooses a workflow hint from explicit
phrases, then leaves evidence reading and every write decision to the agent's
existing tool contracts.  A wrong or ambiguous match therefore cannot turn a
read request into an automatic document update.
"""

from __future__ import annotations

import re
from typing import Any


_ROUTE_DEFINITIONS = {
    "new_paper_impact_review": {
        "label": "新论文综合审查",
        "instruction": (
            "这是一项先审查、后修改的复合任务：先唯一定位用户所说的新导入论文；"
            "论文标题或 paper_id 不明确时，列出已索引论文并要求用户指定，不能按“最新”猜测。"
            "定位后调用 review_new_paper_impact_on_research_document，一次取得研究内容重叠、"
            "研究方案与可行性、五维创新性候选的只读证据包。"
            "本轮只输出综合结论和逐章节拟修改项，明确请求用户确认；"
            "不得调用任何档案或账本写入工具。"
        ),
    },
    "innovation_review": {
        "label": "创新性审查",
        "instruction": (
            "对档案与用户指定的本地论文做只读五维创新性审查。"
            "论文不足时先要求用户指定，不得自动改写档案或账本。"
        ),
    },
    "ledger": {
        "label": "证据与假设账本",
        "instruction": (
            "先读取账本及必要章节和论文证据，提出可确认的研究判断、认识状态和可证伪条件；"
            "取得用户确认前不得写入账本。"
        ),
    },
    "paper_comparison": {
        "label": "论文比对",
        "instruction": (
            "对档案和用户指定的本地论文做全覆盖只读比对；"
            "论文范围不明确时先追问，不得修改档案。"
        ),
    },
    "safe_patch": {
        "label": "安全修改",
        "instruction": (
            "先列出章节并只读取需要修改的部分，说明拟改内容；"
            "取得用户明确确认后，才以 revision 和 section hash 提交局部补丁。"
        ),
    },
    "full_patch": {
        "label": "确认全部写入",
        "instruction": (
            "用户已明确确认将此前列出的全部修改写入当前档案。"
            "这是机械提交，不得重做论文检索、创新审查或修改方案讨论。"
            "先调用 prepare_research_document_patch_context 一次获取四章完整正文、revision 与 hash；"
            "若它提示正文过长，解释必须按章节提交，不得写入。"
            "否则下一轮只调用一次 apply_research_document_patch，以最多四项操作原子提交所有已确认修改；"
            "工具成功后直接结束，不再调用其他工具或核验。"
        ),
    },
    "consult": {
        "label": "档案咨询",
        "instruction": (
            "按问题需要读取相关章节或论文证据并回答；"
            "没有用户明确确认，不得修改档案、账本或版本。"
        ),
    },
}

_INNOVATION = re.compile(r"创新性|新颖性|原创性|创新.{0,4}(审查|评估)|是否.{0,4}创新|novelty", re.IGNORECASE)
_LEDGER = re.compile(r"账本|证据.{0,5}(假设|主张|判断)|假设.{0,5}(证据|记录)|可证伪|研究判断", re.IGNORECASE)
_COMPARISON = re.compile(r"比对|对比|比较|重叠|借鉴|相似|冲突|差异", re.IGNORECASE)
_PATCH = re.compile(r"修改|改成|替换|删除|新增|补充|更新|完善|重写|调整|合并|拆分|写入", re.IGNORECASE)
_NEW_PAPER = re.compile(r"(?:新(?:导入|引入|加入|添加)|刚(?:刚)?导入|新增).{0,12}论文|新论文|论文.{0,12}(?:新(?:导入|引入|加入|添加)|刚(?:刚)?导入|新增)", re.IGNORECASE)
_FULL_PATCH = re.compile(r"(?:全部|所有|上述|以上).{0,12}(?:写入|提交|保存|更新|修改)|(?:确认|同意|可以).{0,10}(?:写入|提交|保存)", re.IGNORECASE)
_ROUTED_INTENT = re.compile(r"^route=([a-z_]+)$", re.MULTILINE)

# The first pass of a new-paper impact review has no write authority.  This
# enforcement lives beside the routing convention so the chat runtime can
# constrain the model rather than trusting an instruction in its prompt.
_ROUTE_TOOL_SCOPES = {
    "new_paper_impact_review": frozenset({
        "list_indexed_papers",
        "review_new_paper_impact_on_research_document",
    }),
    # A request made from an open dossier already includes its document_id.
    # Keeping a confirmed patch inside this small capability set prevents the
    # model from spending the foreground write turn re-running paper research
    # or searching for a document that has already been selected in the UI.
    "safe_patch": frozenset({
        "list_research_document_sections",
        "read_research_document_section",
        "apply_research_document_patch",
    }),
    "full_patch": frozenset({
        "prepare_research_document_patch_context",
        "apply_research_document_patch",
    }),
}


def classify_research_document_intent(request: str) -> str:
    """Return one of the small, stable dossier workflow hints."""
    text = " ".join(str(request or "").split())
    # A newly imported paper plus a request to update a dossier has a stable
    # product meaning: assess its impact before proposing any edit.  This is
    # intentionally a deterministic convention, not a claim that a model can
    # infer every unstated subtask from arbitrary natural language.
    if _NEW_PAPER.search(text) and _PATCH.search(text):
        return "new_paper_impact_review"
    if _FULL_PATCH.search(text):
        return "full_patch"
    if _INNOVATION.search(text):
        return "innovation_review"
    if _LEDGER.search(text):
        return "ledger"
    if _COMPARISON.search(text):
        return "paper_comparison"
    if _PATCH.search(text):
        return "safe_patch"
    return "consult"


def routed_research_document_intent(message: str) -> str | None:
    """Return the trusted route intent embedded by the dossier API, if any."""
    text = str(message or "")
    if "【科研档案路由】" not in text:
        return None
    matched = _ROUTED_INTENT.search(text)
    if matched is None:
        return None
    return matched.group(1)


def allowed_tools_for_routed_request(message: str) -> frozenset[str] | None:
    """Return the least-privilege tool scope for a trusted route envelope.

    A new-paper review remains read-only.  A safe patch can only inspect its
    target dossier and submit the existing checked patch: the selected
    document ID is already present in the route envelope, so global search and
    paper-analysis tools are unnecessary during a confirmed write turn.
    """
    intent = routed_research_document_intent(message)
    return _ROUTE_TOOL_SCOPES.get(intent) if intent else None


def route_research_document_request(document: dict[str, Any], request: str) -> dict[str, str]:
    """Build a transparent, model-visible context envelope for one dossier."""
    document_id = str(document.get("document_id") or "").strip()
    title = str(document.get("title") or "科研档案").strip() or "科研档案"
    user_request = " ".join(str(request or "").split())
    if not document_id:
        raise ValueError("research document id is required")
    if not user_request:
        raise ValueError("research document request is required")
    intent = classify_research_document_intent(user_request)
    definition = _ROUTE_DEFINITIONS[intent]
    message = (
        "【科研档案路由】\n"
        f"document_id={document_id}\n"
        f"档案标题={title}\n"
        f"route={intent}\n"
        f"流程提示={definition['instruction']}\n\n"
        f"用户请求：{user_request}"
    )
    return {
        "document_id": document_id,
        "intent": intent,
        "label": str(definition["label"]),
        "message": message,
    }
