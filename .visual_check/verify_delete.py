"""End-to-end verification for the research-archive delete feature.

Two checks:
  1. The DELETE /api/v1/workspace/research-documents/{id} endpoint actually
     removes a dossier directory (uses a throwaway hex id, never real docs).
  2. The 7860-served frontend bundle contains the delete UI + API call, proving
     the rebuilt image shipped the new code.

Run: python .visual_check/verify_delete.py
"""

from __future__ import annotations

import os
import re
import sys
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:7860"
API = f"{BASE}/api/v1"
RUNTIME_DIR = Path(__file__).resolve().parents[1] / "runtime" / "primary" / "research_documents"
# The store's id format is `research-doc-<12 lowercase hex chars>`; the directory
# name and the API id are the same string.
TEST_ID = "research-doc-a1b2c3d4e5f6"


def _http(method: str, url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, headers={"X-API-Key": ""})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        return exc.code, exc.read()


def verify_backend_delete() -> bool:
    target = RUNTIME_DIR / TEST_ID
    # Clean any leftover, then create a throwaway dossier directory.
    if target.exists():
        import shutil
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    (target / "metadata.json").write_text(
        '{"document_id": "%s", "title": "verify", "markdown_file": "document.md"}' % TEST_ID,
        encoding="utf-8",
    )
    (target / "document.md").write_text("# verify\n", encoding="utf-8")

    status, _ = _http("DELETE", f"{API}/workspace/research-documents/{TEST_ID}")
    removed = not target.exists()
    print(f"[backend] DELETE status={status} dir_removed={removed}")
    # Tidy up if the endpoint failed so we never leave test residue.
    if target.exists():
        import shutil
        shutil.rmtree(target)
    return status == 204 and removed


def verify_frontend_deployed() -> bool:
    status, html = _http("GET", f"{BASE}/")
    if status != 200:
        print(f"[frontend] index.html status={status}")
        return False
    html_text = html.decode("utf-8", "replace")
    assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', html_text)
    js_ok = css_ok = False
    for asset in assets:
        _, body = _http("GET", f"{BASE}{asset}")
        text = body.decode("utf-8", "replace")
        if asset.endswith(".js"):
            # Production builds minify and may rename the exported helper, so we
            # rely on stable string literals that survive minification: the
            # delete button's className and the DELETE fetch verb + path.
            if "document-delete" in text and "DELETE" in text and "workspace/research-documents/" in text:
                js_ok = True
        elif asset.endswith(".css"):
            if ".document-delete" in text and ".document-item" in text:
                css_ok = True
    print(f"[frontend] bundle has delete API+UI js={js_ok} css={css_ok}")
    return js_ok and css_ok


def main() -> int:
    print(f"Runtime dir: {RUNTIME_DIR}")
    backend = verify_backend_delete()
    frontend = verify_frontend_deployed()
    ok = backend and frontend
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
