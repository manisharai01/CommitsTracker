#!/usr/bin/env python3
"""Web UI for the GitHub contribution report.

Opens a page where you enter GitHub usernames and tokens (the same values
.env holds), choose the options and time range, and download the finished
report as a PDF.  See github_contrib/webapp.py for how runs are executed.

Examples
--------
    # On your own computer (127.0.0.1, may use the tokens in .env)
    python webui.py
    python webui.py --port 9000 --no-browser

    # Hosted for other people, behind an HTTPS reverse proxy
    python webui.py --public-url https://reports.example.com --trust-proxy
"""

from __future__ import annotations

import argparse
import ssl
from pathlib import Path

from github_contrib.logging_config import setup_logging
from github_contrib.webapp import (
    DEFAULT_HOST,
    DEFAULT_JOBS_DIR,
    DEFAULT_PORT,
    Settings,
    public_settings,
    serve,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="webui.py",
        description="Serve the contribution report web UI.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="Interface to listen on. Local mode only allows the loopback default.",
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

    hosted = parser.add_argument_group(
        "public mode",
        "Serve many users: each browser session only sees its own reports, tokens are "
        "always typed in (.env is never read), and runs are limited and expire.",
    )
    hosted.add_argument(
        "--public-url",
        metavar="URL",
        help="The site's public HTTPS address, e.g. https://reports.example.com. "
        "Turns on public mode; requests from any other origin are refused.",
    )
    hosted.add_argument("--parallel", type=int, help="Runs executed at the same time (public default 2).")
    hosted.add_argument(
        "--job-timeout",
        type=float,
        metavar="MINUTES",
        help="Stop a run after this many minutes (public default 120; 0 = no limit).",
    )
    hosted.add_argument(
        "--retention-hours",
        type=float,
        help="Delete reports this many hours after they finish (public default 24; 0 = keep).",
    )
    hosted.add_argument(
        "--max-active-per-user",
        type=int,
        help="Reports one browser session may have queued or running (public default 2).",
    )
    hosted.add_argument("--max-queue", type=int, help="Reports queued or running in total (public default 50).")
    hosted.add_argument(
        "--max-jobs-per-hour",
        type=int,
        help="Reports one client address may start per hour (public default 20; 0 = no limit).",
    )
    hosted.add_argument(
        "--trust-proxy",
        action="store_true",
        help="Take the client address from X-Forwarded-For (set by your reverse proxy).",
    )
    hosted.add_argument("--tls-cert", type=Path, help="Serve HTTPS directly with this certificate (PEM).")
    hosted.add_argument("--tls-key", type=Path, help="Private key for --tls-cert.")
    args = parser.parse_args(argv)

    if args.public_url:
        try:
            settings = public_settings(
                args.public_url,
                jobs_dir=args.output,
                parallel=args.parallel,
                job_timeout=None if args.job_timeout is None else args.job_timeout * 60,
                retention_hours=args.retention_hours,
                max_active_per_user=args.max_active_per_user,
                max_queue=args.max_queue,
                max_jobs_per_hour=args.max_jobs_per_hour,
                trust_proxy=args.trust_proxy,
            )
        except ValueError as exc:
            parser.error(str(exc))
    else:
        if args.trust_proxy or args.tls_cert:
            parser.error("--trust-proxy and --tls-cert need --public-url.")
        settings = Settings(
            jobs_dir=args.output,
            parallel=args.parallel or 1,
            job_timeout=(args.job_timeout or 0) * 60,
            retention_hours=args.retention_hours or 0,
        )

    ssl_context = None
    if args.tls_cert:
        if not args.tls_key:
            parser.error("--tls-cert needs --tls-key.")
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_context.load_cert_chain(args.tls_cert, args.tls_key)

    setup_logging("INFO")
    try:
        serve(
            args.host,
            args.port,
            settings=settings,
            open_browser=not args.no_browser,
            ssl_context=ssl_context,
        )
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
