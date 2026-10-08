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

    # Hosted with "Sign in with GitHub" and a report history (see DEPLOY.md):
    # set GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, SESSION_SECRET and
    # DATABASE_URL in the environment (or .env), then run it in public mode.
    # To try that on your own computer:
    python webui.py --public-url http://localhost:8765

On Render (or any host that sets PORT), the defaults come from the
environment: --port from $PORT, --public-url from $PUBLIC_URL or
$RENDER_EXTERNAL_URL, and the server listens on 0.0.0.0 behind the proxy.
"""

from __future__ import annotations

import argparse
import os
import ssl
from pathlib import Path
from typing import Mapping

from github_contrib.logging_config import get_logger, setup_logging
from github_contrib.webapp import (
    DEFAULT_HOST,
    DEFAULT_JOBS_DIR,
    DEFAULT_PORT,
    Settings,
    public_settings,
    serve,
)

log = get_logger("webui")

#: Settings webui.py also takes from the project's .env file, so sign-in can be
#: tried locally (a real environment variable wins). Only these: the GitHub
#: tokens .env holds must never reach a public-mode server.
DOTENV_SETTINGS = (
    "PUBLIC_URL",
    "DATABASE_URL",
    "GITHUB_CLIENT_ID",
    "GITHUB_CLIENT_SECRET",
    "SESSION_SECRET",
    "GITHUB_OAUTH_SCOPES",
)


def _env(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


def _with_dotenv_settings(env: Mapping[str, str], path: Path) -> dict[str, str]:
    """``env`` plus the :data:`DOTENV_SETTINGS` that only ``path`` (.env) sets."""
    merged = dict(env)
    try:
        from dotenv import dotenv_values  # type: ignore import-not-found

        values = dotenv_values(path)
    except Exception:  # python-dotenv missing, or .env unreadable
        return merged
    for key in DOTENV_SETTINGS:
        if not _env(merged, key) and (values.get(key) or "").strip():
            merged[key] = str(values[key])
    return merged


def build_parser(env: Mapping[str, str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="webui.py",
        description="Serve the contribution report web UI.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Interface to listen on. Local mode only allows the loopback default. "
        "Public mode defaults to $HOST, else 0.0.0.0 when $PORT or $RENDER is set.",
    )
    parser.add_argument(
        "--port",
        type=int,
        # A string default is converted by type=int (and rejected if invalid).
        default=_env(env, "PORT") or DEFAULT_PORT,
        help="Port to listen on. $PORT, when set, replaces the built-in default.",
    )
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
        "always typed in (.env is never read), and runs are limited and expire. With "
        "GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET and SESSION_SECRET set, people sign in "
        "with GitHub instead and their history is kept in DATABASE_URL.",
    )
    hosted.add_argument(
        "--public-url",
        metavar="URL",
        default=_env(env, "PUBLIC_URL") or _env(env, "RENDER_EXTERNAL_URL") or None,
        help="The site's public HTTPS address, e.g. https://reports.example.com. "
        "Turns on public mode; requests from any other origin are refused. "
        "Default: $PUBLIC_URL, else $RENDER_EXTERNAL_URL.",
    )
    hosted.add_argument(
        "--parallel",
        type=int,
        default=_env(env, "PARALLEL") or None,
        help="Runs executed at the same time (public default 2; env PARALLEL).",
    )
    hosted.add_argument(
        "--job-timeout",
        type=float,
        metavar="MINUTES",
        help="Stop a run after this many minutes (public default 120; 0 = no limit).",
    )
    hosted.add_argument(
        "--retention-hours",
        type=float,
        default=_env(env, "RETENTION_HOURS") or None,
        help="Delete reports this many hours after they finish "
        "(public default 24; 0 = keep; env RETENTION_HOURS).",
    )
    hosted.add_argument(
        "--max-active-per-user",
        type=int,
        help="Reports one user may have queued or running (public default 2).",
    )
    hosted.add_argument("--max-queue", type=int, help="Reports queued or running in total (public default 50).")
    hosted.add_argument(
        "--max-jobs-per-hour",
        type=int,
        help="Reports one client address (one GitHub account when signed in) may start "
        "per hour (public default 20; 0 = no limit).",
    )
    hosted.add_argument(
        "--trust-proxy",
        action="store_true",
        default=None,
        help="Take the client address from X-Forwarded-For (set by your reverse proxy). "
        "On by default when $RENDER is set.",
    )
    hosted.add_argument("--tls-cert", type=Path, help="Serve HTTPS directly with this certificate (PEM).")
    hosted.add_argument("--tls-key", type=Path, help="Private key for --tls-cert.")
    return parser


def configure(
    argv: list[str] | None = None, env: Mapping[str, str] | None = None
) -> tuple[argparse.ArgumentParser, argparse.Namespace, str, Settings]:
    """Parse the command line (with defaults from ``env``) into
    ``(parser, args, host, settings)``. Exits with a usage error on bad input."""
    if env is None:
        env = _with_dotenv_settings(os.environ, Path(__file__).resolve().parent / ".env")
    parser = build_parser(env)
    args = parser.parse_args(argv)
    on_platform = bool(_env(env, "PORT") or _env(env, "RENDER"))

    if args.public_url:
        trust_proxy = args.trust_proxy if args.trust_proxy is not None else bool(_env(env, "RENDER"))
        client_id = _env(env, "GITHUB_CLIENT_ID")
        client_secret = _env(env, "GITHUB_CLIENT_SECRET")
        if bool(client_id) != bool(client_secret):
            log.warning(
                "Set both GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET to turn on GitHub "
                "sign-in; with only one of them, people paste tokens instead."
            )
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
                trust_proxy=trust_proxy,
                github_client_id=client_id,
                github_client_secret=client_secret,
                session_secret=(env.get("SESSION_SECRET") or "").strip(),
                oauth_scopes=_env(env, "GITHUB_OAUTH_SCOPES") or None,
                database_url=_env(env, "DATABASE_URL"),
            )
        except ValueError as exc:
            parser.error(str(exc))
        if not settings.auth:
            log.warning(
                "GitHub sign-in is off: set GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET (from a "
                "GitHub OAuth App) to turn it on. Until then people paste tokens%s.",
                ", and DATABASE_URL is not used" if _env(env, "DATABASE_URL") else "",
            )
        # Behind a platform proxy (Render sets PORT and RENDER) listen on every interface.
        host = args.host or _env(env, "HOST") or ("0.0.0.0" if on_platform else DEFAULT_HOST)
    else:
        if args.trust_proxy or args.tls_cert:
            parser.error("--trust-proxy and --tls-cert need --public-url.")
        settings = Settings(
            jobs_dir=args.output,
            parallel=args.parallel or 1,
            job_timeout=(args.job_timeout or 0) * 60,
            retention_hours=args.retention_hours or 0,
        )
        # Local mode only ever listens on loopback unless told otherwise.
        host = args.host or DEFAULT_HOST
        if _env(env, "GITHUB_CLIENT_ID") or _env(env, "DATABASE_URL"):
            log.info(
                "Local mode: no GitHub sign-in or report history (tokens come from the form "
                "or .env). For sign-in, set PUBLIC_URL=http://localhost:%d in .env or pass "
                "--public-url http://localhost:%d.",
                args.port, args.port,
            )
    return parser, args, host, settings


def main(argv: list[str] | None = None) -> int:
    setup_logging("INFO")
    parser, args, host, settings = configure(argv)

    ssl_context = None
    if args.tls_cert:
        if not args.tls_key:
            parser.error("--tls-cert needs --tls-key.")
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_context.load_cert_chain(args.tls_cert, args.tls_key)

    try:
        serve(
            host,
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
