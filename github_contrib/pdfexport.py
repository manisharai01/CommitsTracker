"""PDF export of the HTML report via a headless Chromium browser.

Microsoft Edge ships with Windows 10/11, and Edge, Chrome and Chromium's
headless shell all support ``--headless --print-to-pdf``.  The report is opened
with the ``#print`` fragment so its script expands the chronological commit
appendix — the PDF therefore contains the complete commit history.

The browser holds a whole document in memory while it prints it: a few hundred
MB for the report itself plus about 0.25 MB per appendix row, so one long
report (1,700+ commits: over 1 GB) was enough to take a 512 MB server down.
A long appendix is therefore printed in parts of ``PDF_PART_ROWS`` rows
(``#print=FROM-TO``) that pypdf joins into one PDF: the memory needed no longer
grows with the number of commits. On Linux the browser is also stopped before
the server runs out of memory, so a PDF that can't be made never takes the
finished report down with it.

Printing a long report is CPU-heavy (about 8 CPU-seconds for 1,800 commits),
so it takes minutes on a small cloud instance (0.1 CPU). The browser gets a
generous time limit (``PDF_TIMEOUT`` seconds, default 15 minutes), a throwaway
profile, and none of its background services.

``python -m github_contrib.pdfexport <report.html>`` renders one report; the
web UI runs it once a report has finished, after the report process exited.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .logging_config import get_logger

log = get_logger("pdfexport")

DEFAULT_TIMEOUT_S = 900.0
#: Appendix rows per printed part (``PDF_PART_ROWS``; 0 prints in one go).
DEFAULT_PART_ROWS = 200
#: The browser is stopped when the server gets this close to its memory limit:
#: running out would restart the whole server, finished report and all.
MEMORY_HEADROOM = 64 * 1024 * 1024
#: Exit status of ``python -m github_contrib.pdfexport`` when the PDF needed
#: more memory than the server has.
EXIT_NO_MEMORY = 3
_POLL_S = 0.1  # memory can grow ~50 MB in 0.25 s on a fast CPU
_MB = 1024 * 1024

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

#: The commit appendix of a report that can be printed in parts (htmlreport
#: marks it), and its table rows: month headers and commits.
_APPENDIX_RE = re.compile(r"<section id='commit-timeline' data-print-parts>.*?<tbody>(.*?)</tbody>", re.S)
_ROW_RE = re.compile(r"<tr(?: class='(month)')?>")


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
    # The headless shell first: it prints the same PDF with far less memory.
    for name in (
        "chromium-headless-shell", "chrome-headless-shell",
        "msedge", "chrome", "google-chrome", "chromium", "chromium-browser",
    ):
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


def part_rows() -> int:
    """Appendix rows per printed part: ``PDF_PART_ROWS``, else 200 (0 = all at once)."""
    try:
        value = int(os.environ.get("PDF_PART_ROWS") or DEFAULT_PART_ROWS)
    except ValueError:
        return DEFAULT_PART_ROWS
    return max(value, 0)


def appendix_rows(html: str) -> list[bool]:
    """The commit appendix's table rows, True for a month header; ``[]`` when
    the report can't be printed in parts (no appendix, or an older report)."""
    match = _APPENDIX_RE.search(html)
    return [bool(month) for month in _ROW_RE.findall(match.group(1))] if match else []


def plan_parts(rows: list[bool], size: int) -> list[tuple[int, int]]:
    """Split the appendix rows into parts [start, end) of at most ``size`` rows,
    each cut just before a month header when one falls in its second half. A
    tail of under a quarter part joins the last part instead of making its own."""
    parts: list[tuple[int, int]] = []
    start = 0
    while start < len(rows):
        end = min(start + size, len(rows))
        if len(rows) - end < size // 4:
            end = len(rows)
        elif end < len(rows):
            end = next((i for i in range(end, start + size // 2, -1) if rows[i]), end)
        parts.append((start, end))
        start = end
    return parts


def browser_command(
    browser: str,
    html_path: Path,
    pdf_path: Path,
    profile: Path,
    headless: str = "--headless=new",
    fragment: str = "print",
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
    # (see htmlreport._SCRIPT), before the browser prints it; '#print=FROM-TO'
    # prints one part of it.
    cmd += [f"--print-to-pdf={pdf_path.resolve()}", html_path.resolve().as_uri() + "#" + fragment]
    return cmd


def _read(path: str) -> str:
    try:
        with open(path, encoding="ascii", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _field(text: str, key: str) -> int:
    """The number after ``key`` at the start of a line (/proc and cgroup files)."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == key and parts[1].isdigit():
            return int(parts[1])
    return 0


def memory_use() -> tuple[int, int] | None:
    """(bytes in use, limit) for this server — its container's memory cgroup,
    else the machine — or ``None`` where unknown (not Linux).
    ``PDF_MEMORY_LIMIT_MB`` overrides the limit. In use leaves out inactive
    file cache, which the kernel drops before it runs out of memory."""
    used = limit = 0
    for folder, usage_file, limit_file, inactive in (
        ("/sys/fs/cgroup/", "memory.current", "memory.max", "inactive_file"),  # cgroup v2
        ("/sys/fs/cgroup/memory/", "memory.usage_in_bytes", "memory.limit_in_bytes", "total_inactive_file"),
    ):
        usage = _read(folder + usage_file).strip()
        if usage.isdigit():
            used = max(int(usage) - _field(_read(folder + "memory.stat"), inactive), 0)
            cap = _read(folder + limit_file).strip()  # "max", or a huge number: no limit
            limit = int(cap) if cap.isdigit() and int(cap) < 1 << 50 else 0
            break
    meminfo = _read("/proc/meminfo")
    total = _field(meminfo, "MemTotal:") * 1024
    if not used and total:
        used = total - _field(meminfo, "MemAvailable:") * 1024
    limit = limit or total
    override = os.environ.get("PDF_MEMORY_LIMIT_MB", "").strip()
    if override.isdigit() and int(override) > 0:
        limit = int(override) * _MB
    return (used, limit) if used and limit else None


def _descendants(pid: int) -> list[int]:
    """Every process ``pid`` started, and the ones those started (Linux)."""
    try:
        names = os.listdir("/proc")
    except OSError:
        return []
    children: dict[int, list[int]] = {}
    for name in names:
        if name.isdigit():
            try:
                ppid = int(_read(f"/proc/{name}/stat").rsplit(")", 1)[1].split()[1])
            except (IndexError, ValueError):
                continue
            children.setdefault(ppid, []).append(int(name))
    found: list[int] = []
    todo = [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            found.append(child)
            todo.append(child)
    return found


def _stop(process: subprocess.Popen) -> None:
    """Kill the browser and every process it started."""
    if process.poll() is None:
        try:
            if sys.platform.startswith("win"):
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                    capture_output=True, timeout=15, check=False,
                )
            else:
                for pid in [*_descendants(process.pid), process.pid]:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()


def _run_browser(cmd: list[str], log_path: Path, deadline: float) -> tuple[str, int | None]:
    """Run one print to its end: ("done", exit code), or ("timeout" | "memory",
    None) when the browser had to be stopped — past ``deadline``, or with the
    server about to run out of memory."""
    with open(log_path, "wb") as err:  # a file, not a pipe: a full pipe would stall the browser
        process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err)
    try:
        while True:
            try:
                return "done", process.wait(timeout=_POLL_S)
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() >= deadline:
                return "timeout", None
            use = memory_use()
            if use is not None and use[0] > use[1] - MEMORY_HEADROOM:
                log.warning(
                    "PDF export stopped: the server was about to run out of memory (%d of %d MB in use). "
                    "Set PDF_PART_ROWS lower, or give the server more memory.",
                    use[0] // _MB, use[1] // _MB,
                )
                return "memory", None
    finally:
        _stop(process)


def _fragments(html_path: Path) -> list[str]:
    """The URL fragments to print, one PDF part each (see the module docstring)."""
    size = part_rows()
    try:
        rows = appendix_rows(html_path.read_text(encoding="utf-8", errors="replace")) if size else []
    except OSError:
        rows = []
    if len(rows) <= size:
        return ["print"]
    if importlib.util.find_spec("pypdf") is None:
        log.warning("pypdf is not installed: printing the PDF in one go, which needs far more memory.")
        return ["print"]
    return [f"print={start}-{end}" for start, end in plan_parts(rows, size)]


def _join(parts: list[Path], target: Path) -> None:
    """Concatenate PDFs into ``target``, keeping the first one's title."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for part in parts:
        writer.append(part)
    info = PdfReader(parts[0]).metadata
    if info:
        writer.add_metadata({key: value for key, value in info.items() if isinstance(value, str)})
    with open(target, "wb") as fh:
        writer.write(fh)


def _export(html_path: Path, pdf_path: Path | None, timeout: float | None) -> tuple[Path | None, str]:
    """:func:`export_pdf`, also saying how it ended: "done", "failed",
    "timeout" or "memory"."""
    html_path = Path(html_path)
    pdf_path = html_path.with_suffix(".pdf") if pdf_path is None else Path(pdf_path)
    if not html_path.exists():
        log.warning("cannot export PDF: %s does not exist", html_path)
        return None, "failed"

    browser = find_browser()
    if browser is None:
        log.warning(
            "No Edge/Chrome found for PDF export. Open %s in a browser and use "
            "'Print > Save as PDF' instead.", html_path
        )
        return None, "failed"

    limit = pdf_timeout() if timeout is None else timeout
    started = time.monotonic()
    fragments = _fragments(html_path)
    # Old headless mode (pre-109 Chromium) is the fallback when the new one
    # writes nothing; the first part settles which mode the others use.
    modes = ["--headless=new", "--headless"]
    with tempfile.TemporaryDirectory(prefix="ct-pdf-", ignore_cleanup_errors=True) as tmp:
        work = Path(tmp)
        printed: list[Path] = []
        for index, fragment in enumerate(fragments):
            part = work / f"part-{index:03d}.pdf"
            code = None
            for headless in modes:
                cmd = browser_command(browser, html_path, part, work / "profile", headless, fragment)
                try:
                    outcome, code = _run_browser(cmd, work / "browser.log", started + limit)
                except OSError as exc:
                    log.warning("PDF export failed to run %s: %s", Path(browser).name, exc)
                    return None, "failed"
                if outcome == "timeout":
                    log.warning(
                        "PDF export stopped: %s took longer than %.0f seconds (set PDF_TIMEOUT to allow more).",
                        Path(browser).name, limit,
                    )
                if outcome != "done":
                    return None, outcome
                if part.is_file() and part.stat().st_size > 0:
                    modes = [headless]
                    break
            else:
                errors = _read(str(work / "browser.log"))
                log.warning("PDF export produced no output (browser exit code %s): %s", code, errors.strip()[:400])
                return None, "failed"
            printed.append(part)

        # Written next to the target, then renamed: never a half-written PDF.
        partial = pdf_path.with_name(pdf_path.name + ".partial")
        try:
            if len(printed) == 1:
                shutil.copyfile(printed[0], partial)
            else:
                _join(printed, partial)
            os.replace(partial, pdf_path)
        except Exception as exc:  # noqa: BLE001 - pypdf raises many kinds; the report must survive
            log.warning("PDF export could not save %s: %s", pdf_path.name, exc)
            partial.unlink(missing_ok=True)
            return None, "failed"

    in_parts = f", {len(printed)} parts" if len(printed) > 1 else ""
    log.info("wrote %s (via %s%s, %.0fs)", pdf_path.name, Path(browser).name, in_parts, time.monotonic() - started)
    return pdf_path, "done"


def export_pdf(html_path: Path, pdf_path: Path | None = None, *, timeout: float | None = None) -> Path | None:
    """Render ``html_path`` to ``pdf_path`` (default: same name, .pdf).

    Returns the written path, or ``None`` when no browser is available or the
    conversion fails (a warning is logged either way — PDF export must never
    break the main run).
    """
    return _export(html_path, pdf_path, timeout)[0]


def main(argv: list[str] | None = None) -> int:
    """``python -m github_contrib.pdfexport <report.html>``: 0 once the PDF is
    written, :data:`EXIT_NO_MEMORY` when the server ran short of memory, else 1."""
    from .logging_config import setup_logging

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m github_contrib.pdfexport <report.html>", file=sys.stderr)
        return 2
    setup_logging("INFO")
    path, outcome = _export(Path(args[0]), None, None)
    if path is not None:
        return 0
    return EXIT_NO_MEMORY if outcome == "memory" else 1


if __name__ == "__main__":
    raise SystemExit(main())
