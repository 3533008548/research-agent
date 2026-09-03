"""Explicitly download and validate the optional local RAG reranker."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="cross-encoder/mmarco-mMiniLMv2-L12-H384-v1")
    args = parser.parse_args(argv)
    from sentence_transformers import CrossEncoder

    model = CrossEncoder(args.model, max_length=512)
    scores = model.predict(
        [("What is the proposed method?", "The proposed method is a graph neural network.")],
        show_progress_bar=False,
    )
    print(f"本地重排模型已就绪: {args.model}（自检分数 {float(scores[0]):.4f}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
