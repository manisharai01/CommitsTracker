"""PDF export of the HTML report via a headless Chromium browser.

No extra Python dependencies: Microsoft Edge ships with Windows 10/11 and both
Edge and Chrome support ``--headless --print-to-pdf``.  The report is opened
with the ``#print`` fragment so its script expands the chronological commit
appendix — the PDF therefore contains the complete commit history.

Printing a long report is CPU-heavy (about 8 CPU-seconds for 1,800 commits),
so it takes minutes on a small cloud instance (0.1 CPU). The browser gets a
generous time limit (``PDF_TIMEOUT`` seconds, default 15 minutes), a throwaway
profile, and none of its background services.

``python -m github_contrib.pdfexport <report.html>`` renders one report; the
web UI runs it once a report has finished, after the report process exited.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .logging_config import get_logger

log = get_logger("pdfexport")

DEFAULT_TIMEOUT_S = 900.0

#: Background services a one-off print never needs: each costs CPU and a
#: process, which matters a lot on a 0.1-CPU host.
_LEAN_FLAGS = (
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-sync",
    "--no-service-autorun",
    "--metrics-recording-only",
    "--mute-audio",
    "--no-pings",
    "--disable-features=Translate,MediaRouter,OptimizationHints,"
    "AutofillServerCommunication,CalculateNativeWinOcclusion",
)


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


def pdf_timeout() -> float:
    """Seconds the browser may take: ``PDF_TIMEOUT``, else 15 minutes."""
    try:
        value = float(os.environ.get("PDF_TIMEOUT") or DEFAULT_TIMEOUT_S)
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return value if value > 0 else DEFAULT_TIMEOUT_S


def browser_command(
    browser: str, html_path: Path, pdf_path: Path, profile: Path, headless: str = "--headless=new"
) -> list[str]:
    """The browser invocation that prints ``html_path`` to ``pdf_path``."""
    cmd = [
        browser,
        headless,
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-pdf-header-footer",
        *_LEAN_FLAGS,
        f"--user-data-dir={profile}",
    ]
    if os.environ.get("PDF_NO_SANDBOX") == "1":
        # Containers (the Docker image sets this) can't use Chromium's sandbox.
        # Their /dev/shm is usually 64 MB, too small for a long report.
        cmd += ["--no-sandbox", "--disable-dev-shm-usage"]
    # '#print' makes the report expand its commit appendix as the page loads
    # (see htmlreport._SCRIPT), before the browser prints it.
    cmd += [f"--print-to-pdf={pdf_path.resolve()}", html_path.resolve().as_uri() + "#print"]
    return cmd


def export_pdf(html_path: Path, pdf_path: Path | None = None, *, timeout: float | None = None) -> Path | None:
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

    limit = pdf_timeout() if timeout is None else timeout
    started = time.monotonic()
    result = None
    with tempfile.TemporaryDirectory(prefix="ct-pdf-", ignore_cleanup_errors=True) as profile:
        # Old headless mode (pre-109 Chromium) is the fallback when the new one
        # writes nothing.
        for headless in ("--headless=new", "--headless"):
            cmd = browser_command(browser, html_path, pdf_path, Path(profile), headless)
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=limit)
            except subprocess.TimeoutExpired:
                log.warning(
                    "PDF export stopped: %s took longer than %.0f seconds (set PDF_TIMEOUT to allow more).",
                    Path(browser).name, limit,
                )
                return None
            except OSError as exc:
                log.warning("PDF export failed to run %s: %s", Path(browser).name, exc)
                return None
            if pdf_path.exists() and pdf_path.stat().st_size > 0:
                log.info(
                    "wrote %s (via %s, %.0fs)", pdf_path.name, Path(browser).name, time.monotonic() - started
                )
                return pdf_path

    log.warning(
        "PDF export produced no output (browser exit code %s): %s",
        result.returncode if result else "?", ((result.stderr if result else "") or "").strip()[:400],
    )
    return None


def main(argv: list[str] | None = None) -> int:
    """``python -m github_contrib.pdfexport <report.html>``: 0 once the PDF is written."""
    from .logging_config import setup_logging

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m github_contrib.pdfexport <report.html>", file=sys.stderr)
        return 2
    setup_logging("INFO")
    return 0 if export_pdf(Path(args[0])) else 1


if __name__ == "__main__":
    raise SystemExit(main())
