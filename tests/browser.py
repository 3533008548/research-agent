"""Shared Playwright launch options for browser regression tests."""

from __future__ import annotations

import os
from pathlib import Path


def launch_chromium(playwright):
    """Launch bundled Chromium, or an explicitly configured local executable."""
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "").strip()
    if executable:
        path = Path(executable).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"PLAYWRIGHT_CHROMIUM_EXECUTABLE 不存在: {path}")
        return playwright.chromium.launch(headless=True, executable_path=str(path))
    return playwright.chromium.launch(headless=True)
