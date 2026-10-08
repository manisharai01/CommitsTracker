"""PDF export of the HTML report via a headless Chromium browser.

No extra Python dependencies: Microsoft Edge ships with Windows 10/11 and both
Edge and Chrome support ``--headless --print-to-pdf``.  The report is opened
with the ``#print`` fragment so its script expands every per-repo commit list —
the PDF therefore contains the complete commit history, not the collapsed view.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .logging_config import get_logger

log = get_logger("pdfexport")

_BROWSER_TIMEOUT_S = 180


def _browser_candidates() -> list[str]:
    """Possible headless-capable browser executables, most preferred first."""
    candidates: list[str] = []
    program_dirs = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("LocalAppData", ""),
    ]
    for base in program_dirs:
        if not base:
            continue
        candidates.append(str(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"))
        candidates.append(str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    for name in ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    return candidates


def find_browser() -> str | None:
    """The first available Edge/Chrome/Chromium executable, or ``None``."""
    for candidate in _browser_candidates():
        if candidate and Path(candidate).exists():
            return candidate
    return None


def export_pdf(html_path: Path, pdf_path: Path | None = None) -> Path | None:
    """Render ``html_path`` to ``pdf_path`` (default: same name, .pdf).

    Returns the written path, or ``None`` when no browser is available or the
    conversion fails (a warning is logged either way — PDF export must never
    break the main run).
    """
    html_path = Path(html_path)
    if pdf_path is None:
        pdf_path = html_path.with_suffix(".pdf")
    if not html_path.exists():
        log.warning("cannot export PDF: %s does not exist", html_path)
        return None

    browser = find_browser()
    if browser is None:
        log.warning(
            "No Edge/Chrome found for PDF export. Open %s in a browser and use "
            "'Print > Save as PDF' instead.", html_path
        )
        return None

    # '#print' makes the report expand all commit lists (see htmlreport._SCRIPT).
    url = html_path.resolve().as_uri() + "#print"
    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-pdf-header-footer",
        # Give the page's script time to run before the PDF snapshot is taken.
        "--virtual-time-budget=5000",
        f"--print-to-pdf={pdf_path.resolve()}",
        url,
    ]
    if os.environ.get("PDF_NO_SANDBOX") == "1":
        # Containers (the Docker image sets this) can't use Chromium's sandbox.
        # Their /dev/shm is usually 64 MB, too small for a long report.
        cmd[2:2] = ["--no-sandbox", "--disable-dev-shm-usage"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_BROWSER_TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("PDF export failed to run %s: %s", Path(browser).name, exc)
        return None

    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        # Old headless mode (pre-109 Chromium) uses --headless without '=new'.
        cmd[1] = "--headless"
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_BROWSER_TIMEOUT_S
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("PDF export failed to run %s: %s", Path(browser).name, exc)
            return None

    if pdf_path.exists() and pdf_path.stat().st_size > 0:
        log.info("wrote %s (via %s)", pdf_path.name, Path(browser).name)
        return pdf_path

    log.warning(
        "PDF export produced no output (browser exit code %s): %s",
        result.returncode, (result.stderr or "").strip()[:400],
    )
    return None
