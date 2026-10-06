#!/usr/bin/env python3
"""Web UI for the GitHub contribution report.

Opens a local page where you enter GitHub usernames and tokens (the same values
.env holds), choose the report options and download the finished report as a
PDF.  See github_contrib/webapp.py for how runs are executed.

Examples
--------
    python webui.py                       # serves http://127.0.0.1:8765 and opens it
    python webui.py --port 9000 --no-browser
"""

from __future__ import annotations

import argparse
from pathlib import Path

from github_contrib.logging_config import setup_logging
from github_contrib.webapp import DEFAULT_HOST, DEFAULT_JOBS_DIR, DEFAULT_PORT, serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="webui.py",
        description="Serve the contribution report web UI.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="Interface to listen on. Keep the loopback default: the page sends "
        "tokens to this server over plain HTTP.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to listen on.")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_JOBS_DIR,
        help="Directory that receives one folder per report run.",
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="Don't open the page in a browser on start."
    )
    args = parser.parse_args(argv)

    setup_logging("INFO")
    serve(args.host, args.port, jobs_dir=args.output, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
