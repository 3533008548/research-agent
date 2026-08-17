"""Export one reviewed Badcase candidate as a safe evaluation-fixture draft."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from badcase_store import BadcaseStore, promotion_template
from runtime_paths import RuntimePaths


def main() -> int:
    parser = argparse.ArgumentParser(
        description="导出 Badcase 候选的合成评测样例模板（不会自动加入 Git）",
    )
    parser.add_argument("--candidate-id", required=True, help="Badcase 候选 ID，例如 bc-xxxxxxxxxxxx")
    parser.add_argument("--data-dir", default=None, help="运行时目录（默认 APP_DATA_DIR 或 runtime）")
    parser.add_argument("--output", help="可选 JSON 输出路径；省略时写到标准输出")
    args = parser.parse_args()

    paths = RuntimePaths.from_root(args.data_dir)
    paths.ensure_initialized()
    store = BadcaseStore(str(paths.badcases_db))
    try:
        candidate = store.get(args.candidate_id)
    finally:
        store.close()
    if candidate is None:
        print(f"未找到 Badcase 候选：{args.candidate_id}", file=sys.stderr)
        return 2

    draft = promotion_template(candidate)
    text = json.dumps(draft, ensure_ascii=False, indent=2) + "\n"
    if not args.output:
        print(text, end="")
        return 0

    destination = Path(args.output).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    print(f"✅ 已导出合成评测样例草稿：{destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
