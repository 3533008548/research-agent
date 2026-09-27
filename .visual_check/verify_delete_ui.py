"""Non-destructive browser check for the research-archive delete UI.

Asserts the delete affordances actually render on the 7860-served app:
  * each sidebar dossier row has a visible `.document-delete` button
  * the row is the new `.document-item` layout with a `.workspace-list-button`
  * the open dossier header shows a visible `删除档案` button

It NEVER clicks delete (that would destroy real dossiers).
Run: CHECK_URL=http://127.0.0.1:7860/ python .visual_check/verify_delete_ui.py
"""

from __future__ import annotations

import os
import pathlib
import sys

from playwright.sync_api import sync_playwright

EDGE = r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
OUT = pathlib.Path(r"D:/develop/academic/research_agent/.visual_check")
OUT.mkdir(parents=True, exist_ok=True)
URL = os.environ.get("CHECK_URL", "http://127.0.0.1:7860/")


def ensure_panel_open(page, title: str) -> None:
    toggle = page.locator(".workspace-panel-toggle", has_text=title)
    toggle.wait_for(state="visible", timeout=15000)
    panel = page.locator(".workspace-panel", has=toggle)
    cls = panel.get_attribute("class") or ""
    if "open" not in cls.split():
        toggle.click()
    page.wait_for_timeout(300)


def main() -> int:
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
        page.wait_for_selector(".document-item", timeout=15000)

        rows = page.locator(".document-item")
        row_count = rows.count()
        # Every listed dossier must expose a visible delete control.
        delete_buttons = page.locator(".document-item .document-delete")
        del_count = delete_buttons.count()
        visible_del = 0
        for i in range(del_count):
            if delete_buttons.nth(i).is_visible():
                visible_del += 1
        # The row must keep a working select button (title opens the dossier).
        select_count = page.locator(".document-item .workspace-list-button").count()

        print(f"sidebar rows={row_count} delete_buttons={del_count} "
              f"visible_delete={visible_del} select_buttons={select_count}")

        # Open the first dossier and check the header delete button.
        page.locator(".document-item .workspace-list-button").first.click()
        page.wait_for_selector(".document-page", timeout=20000)
        page.wait_for_selector(".document-page-content", timeout=20000)
        page.wait_for_timeout(1200)

        header_delete = page.locator(".document-page-actions button.button-danger")
        header_present = header_delete.count() > 0
        header_visible = header_delete.first.is_visible() if header_present else False
        header_text = header_delete.first.inner_text().strip() if header_present else ""
        print(f"header delete present={header_present} visible={header_visible} text={header_text!r}")

        page.screenshot(path=str(OUT / "delete-ui-1440.png"))
        page.locator(".document-page-heading").screenshot(path=str(OUT / "delete-ui-header.png"))
        print(f"saved {OUT / 'delete-ui-1440.png'} and delete-ui-header.png")

        ok = (
            row_count > 0
            and del_count == row_count
            and visible_del == row_count
            and select_count == row_count
            and header_present
            and header_visible
            and header_text == "删除档案"
        )
        print("UI RESULT:", "PASS" if ok else "FAIL")
        browser.close()
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
