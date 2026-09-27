"""Sample real chunks and emit a RAG retrieval-evaluation skeleton.

Ground truth cannot be invented: a label only counts as a hit when the paper
title, the chunk index and a verbatim substring all match.  This script
therefore samples actual indexed chunks and extracts each anchor from the real
text, leaving only the natural-language query to be authored.

    python scripts/generate_rag_eval_cases.py --per-paper 2 --output cases.json

The generated file is a skeleton and is not loadable by ``load_cases`` until
every ``query`` is filled in (it ships as ``query_hint`` instead).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

# Chunks mentioning method, mechanism or measurement language make better
# retrieval targets than front matter, reference lists or bare captions.
_TERMS = (
    "we propose|we introduce|this paper|algorithm|scheduler|mechanism|polling|"
    "latency|jitter|deadline|bound|theorem|evaluation|results|simulation|we show|"
    "priority|queue|shaper|delay|throughput|wake|control|stability|measurement|"
    "we compare|we evaluate|our approach|contribution"
)
_MIN_CHARS = 250
_MAX_CHARS = 1400
_ANCHOR_WORDS = 12


def _normalised(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _anchor_for(text: str) -> str:
    """Pick a contiguous verbatim span so ``text_contains`` is always a real substring."""
    words = text.split()
    if len(words) <= _ANCHOR_WORDS + 8:
        return " ".join(words[: min(len(words), _ANCHOR_WORDS)])
    start = min(8, max(0, len(words) - _ANCHOR_WORDS))
    return " ".join(words[start : start + _ANCHOR_WORDS])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-paper", type=int, default=2, help="每篇论文采样多少个 chunk")
    parser.add_argument("--output", required=True, help="骨架 JSON 输出路径")
    parser.add_argument("--data-dir", help="运行时根目录；默认使用当前 APP_DATA_DIR/runtime")
    parser.add_argument("--min-chars", type=int, default=_MIN_CHARS)
    parser.add_argument("--max-chars", type=int, default=_MAX_CHARS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.per_paper < 1:
        build_parser().error("--per-paper 必须大于 0")

    from paper_store import PaperStore
    from runtime_paths import RuntimePaths

    paths = (
        RuntimePaths.from_root(args.data_dir) if args.data_dir else RuntimePaths.from_root("runtime")
    )
    paths.ensure_initialized()
    persist_dir = Path(args.data_dir).expanduser().resolve() / "derived" / "chroma" if args.data_dir else None
    store = PaperStore(persist_dir=str(persist_dir) if persist_dir else None)

    cases: list[dict] = []
    try:
        for paper in store.list_papers():
            title = str(paper.get("title") or "")
            chunks = store.get_paper_chunks(str(paper.get("paper_id") or "")) or []
            scored = []
            for chunk in chunks:
                text = " ".join(str(chunk.get("text") or "").split())
                if not (args.min_chars <= len(text) <= args.max_chars):
                    continue
                # chunk 0 is usually the title block; it carries little retrieval signal.
                if int(chunk.get("chunk_index") or 0) == 0:
                    continue
                scored.append((len(re.findall(_TERMS, text, re.I)), int(chunk.get("chunk_index") or 0), text))
            scored.sort(key=lambda item: (-item[0], item[1]))
            for _hits, index, text in scored[: args.per_paper]:
                cases.append({
                    "id": f"C{len(cases) + 1:02d}",
                    "query": "",
                    "query_hint": text[:200],
                    "note": "",
                    "labels": [{
                        "paper_title": title,
                        "chunk_index": index,
                        "text_contains": _anchor_for(text),
                    }],
                })
    finally:
        store._query_executor.shutdown(wait=False, cancel_futures=True)

    payload = {
        "schema_version": 1,
        "note": "骨架：label 已取自真实 chunk，query 需按 query_hint 填写后才能被 load_cases 加载。",
        "cases": cases,
    }
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已生成 {len(cases)} 条骨架用例：{destination}")
    print("下一步：为每条填写 query（参考 query_hint），删除 query_hint 字段后即可评测。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
