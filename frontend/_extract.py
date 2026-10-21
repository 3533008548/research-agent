from __future__ import annotations
import re
from pathlib import Path

APP = Path("src/App.tsx")

def load_lines() -> list[str]:
    return APP.read_text(encoding="utf-8").split("\n")

def block_span(lines: list[str], name: str) -> tuple[int, int]:
    start = next(
        i for i, l in enumerate(lines)
        if l.startswith(f"  async function {name}(") or l.startswith(f"  function {name}(")
    )
    depth = 0
    opened = False
    for i in range(start, len(lines)):
        depth += lines[i].count("{") - lines[i].count("}")
        if "{" in lines[i]:
            opened = True
        if opened and depth == 0:
            return start, i
    raise AssertionError(name)

def collect(lines: list[str], names: list[str]) -> str:
    blocks = []
    for name in names:
        s, e = block_span(lines, name)
        blocks.append((s, e))
    blocks.sort()
    return "\n\n".join("\n".join(lines[s:e + 1]) for s, e in blocks)

def remove_blocks(lines: list[str], names: list[str]) -> list[str]:
    spans = sorted((block_span(lines, n) for n in names), reverse=True)
    for s, e in spans:
        end = e + 1
        if end < len(lines) and lines[end].strip() == "":
            end += 1
        del lines[s:end]
    return lines
