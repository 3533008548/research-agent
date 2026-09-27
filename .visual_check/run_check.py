"""Visual regression: drive the research-agent workbench with system Edge and
capture the 科研档案 (research archive) layout at several viewports.

Checks (round 2):
  * safety note removed          -> .document-safety-note must NOT exist
  * outline is a slim full-width strip, no boxes
                                 -> .document-outline-strip exists, .document-outline-item gone
  * ledger fully visible in rail -> rail itself does not scroll, ledger is clipped-free
"""

from __future__ import annotations

import os
import pathlib

from playwright.sync_api import sync_playwright

EDGE = r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
OUT = pathlib.Path(r"D:/develop/academic/research_agent/.visual_check")
OUT.mkdir(parents=True, exist_ok=True)
# Default to the vite dev server (reads src/ live); set CHECK_URL=http://127.0.0.1:7860/
# to regression-test the artefact actually shipped in the container image.
URL = os.environ.get("CHECK_URL", "http://127.0.0.1:5173/")

SHOTS = [
    ("desktop", 1440, 900),
    ("wide", 1920, 1080),
    ("narrow", 560, 900),
]

LAYOUT_PROBE = """() => {
  const q = (s) => document.querySelector(s);
  const r = (el) => (el ? el.getBoundingClientRect() : null);
  const rail = q('.document-rail');
  const ledger = q('.research-ledger');
  const head = q('.research-ledger-heading');
  const strip = q('.document-outline-strip');
  const card = q('.document-page');
  const content = q('.document-main .document-page-content');
  const versions = q('.document-version-history');
  const chips = document.querySelectorAll('.document-outline-chip');
  const out = {
    hasSafetyNote: !!q('.document-safety-note'),
    hasOutlineStrip: !!strip,
    outlineChips: chips.length,
    legacyOutlineItems: document.querySelectorAll('.document-outline-item').length,
    chipLines: strip ? new Set([...chips].map((c) => Math.round(r(c).top))).size : null,
  };
  if (card) out.cardWidth = Math.round(r(card).width);
  if (strip) out.stripWidth = Math.round(r(strip).width);
  if (rail) {
    out.railHeight = Math.round(r(rail).height);
    out.railSelfScrolls = rail.scrollHeight > rail.clientHeight + 1;
    out.railWidth = Math.round(r(rail).width);
  }
  if (ledger && rail && head) {
    out.ledgerTopInRail = Math.round(r(head).top - r(rail).top);
    out.ledgerHeight = Math.round(r(ledger).height);
    out.ledgerOverflow = ledger.scrollHeight > ledger.clientHeight + 1;
    out.ledgerFullyInsideRail =
      r(ledger).top >= r(rail).top - 1 && r(ledger).bottom <= r(rail).bottom + 1;
    out.ledgerHeadVisible =
      r(head).top >= r(rail).top - 1 && r(head).bottom <= r(rail).bottom + 1;
  }
  if (content && versions) {
    out.contentToVersionGap = Math.round(r(versions).top - r(content).bottom);
  }
  return out;
}"""


def ensure_panel_open(page, title: str) -> None:
    """Expand the workspace panel whose toggle text contains ``title``."""
    toggle = page.locator(".workspace-panel-toggle", has_text=title)
    toggle.wait_for(state="visible", timeout=15000)
    panel = page.locator(".workspace-panel", has=toggle)
    cls = panel.get_attribute("class") or ""
    if "open" not in cls.split():
        toggle.click()
    page.wait_for_timeout(300)


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=EDGE,
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)

        page.wait_for_selector(".workspace-panel-toggle", timeout=20000)

        ensure_panel_open(page, "科研档案")
        page.wait_for_selector(".workspace-list-item", timeout=15000)
        page.locator(".workspace-list-item").first.click()

        page.wait_for_selector(".document-page", timeout=20000)
        page.wait_for_selector(".document-page-content", timeout=20000)
        page.wait_for_function(
            "document.querySelector('.document-page-content') && "
            "document.querySelector('.document-page-content').innerText.trim().length > 40",
            timeout=20000,
        )
        page.wait_for_timeout(1500)

        doc_focus = page.eval_on_selector(
            ".workspace", "el => el.classList.contains('workspace-doc-focus')"
        )
        run_inspector = page.query_selector(".run-inspector")
        inspector_hidden = run_inspector is None or run_inspector.evaluate(
            "el => getComputedStyle(el).display === 'none'"
        )
        print(f"doc_focus={doc_focus} inspector_hidden={inspector_hidden}")

        print("layout-1440", page.evaluate(LAYOUT_PROBE))

        # Rail close-up so the ledger area can be eyeballed.
        page.locator(".document-rail").screenshot(path=str(OUT / "archive-rail-1440.png"))
        print("saved archive-rail-1440.png")

        for name, w, h in SHOTS:
            page.set_viewport_size({"width": w, "height": h})
            page.wait_for_timeout(900)
            path = OUT / f"archive-{name}-{w}x{h}.png"
            page.screenshot(path=str(path))
            print(f"saved {path}")

        # Narrow: stacked document body, ensure no overlap / clip.
        page.set_viewport_size({"width": 560, "height": 900})
        page.wait_for_timeout(700)
        print("layout-560", page.evaluate(LAYOUT_PROBE))
        page.locator(".document-body").scroll_into_view_if_needed()
        page.wait_for_timeout(700)
        page.screenshot(path=str(OUT / "archive-narrow-body-560x900.png"))
        print("saved archive-narrow-body-560x900.png")

        # Narrow: element capture of the whole card (strip wrap + stacked rail).
        page.locator(".document-page").screenshot(path=str(OUT / "archive-narrow-card-560.png"))
        print("saved archive-narrow-card-560.png")

        page.set_viewport_size({"width": 1440, "height": 900})
        page.wait_for_timeout(500)
        page.locator(".document-page").screenshot(path=str(OUT / "archive-element-1440.png"))
        print("saved archive-element-1440.png")

        # Regression guard: closing the document tab restores the run-inspector.
        close_btn = page.locator(".workbench-tab-close").first
        if close_btn.count():
            close_btn.click()
        page.wait_for_timeout(1200)
        inspector_present = page.query_selector(".run-inspector") is not None
        doc_focus_after = page.eval_on_selector(
            ".workspace", "el => el.classList.contains('workspace-doc-focus')"
        )
        print(f"after-close: run_inspector_present={inspector_present} doc_focus={doc_focus_after}")
        page.screenshot(path=str(OUT / "chat-view-1440x900.png"))
        print("saved chat-view-1440x900.png")

        browser.close()
    print("DONE")


if __name__ == "__main__":
    main()
