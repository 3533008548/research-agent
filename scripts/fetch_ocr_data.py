"""Fetch pinned, checksum-verified OCR resources for source and desktop runs."""
from __future__ import annotations

import hashlib
from pathlib import Path
import urllib.request


REVISION = "87416418657359cb625c412a48b6e1d6d41c29bd"
SOURCE = f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{REVISION}"
FILES = {
    "eng.traineddata": "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2",
    "chi_sim.traineddata": "a5fcb6f0db1e1d6d8522f39db4e848f05984669172e584e8d76b6b3141e1f730",
    "LICENSE": "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
}


def ensure_ocr_data(destination: Path | None = None) -> Path:
    target = destination or Path(__file__).resolve().parents[1] / "build" / "ocr" / "tessdata"
    target.mkdir(parents=True, exist_ok=True)
    for name, expected in FILES.items():
        path = target / name
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected:
            continue
        staging = path.with_suffix(path.suffix + ".part")
        for attempt in range(3):
            try:
                url = f"{SOURCE}/{name}" + (f"?download={attempt}" if attempt else "")
                with urllib.request.urlopen(url, timeout=45) as response, staging.open("wb") as output:
                    total = 0
                    while chunk := response.read(64 * 1024):
                        total += len(chunk)
                        if total > 16 * 1024 * 1024:
                            raise ValueError(f"OCR resource exceeds size limit: {name}")
                        output.write(chunk)
                if hashlib.sha256(staging.read_bytes()).hexdigest() != expected:
                    raise ValueError(f"OCR resource checksum mismatch: {name}")
                staging.replace(path)
                break
            except (OSError, ValueError):
                if attempt == 2:
                    raise
            finally:
                staging.unlink(missing_ok=True)
    (target / "NOTICE").write_text(
        "Tesseract tessdata_fast: English and Simplified Chinese language models.\n"
        f"Source: https://github.com/tesseract-ocr/tessdata_fast/tree/{REVISION}\n"
        "Distributed unchanged under Apache License 2.0; see LICENSE.\n",
        encoding="utf-8",
    )
    return target


if __name__ == "__main__":
    print(ensure_ocr_data())
