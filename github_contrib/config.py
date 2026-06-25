"""Configuration loading and application settings.

Tokens are read from environment variables (optionally populated from a local
``.env`` file).  Each tracked GitHub login is mapped to the environment
variable that holds *its* Personal Access Token:

    manisharai01 -> GITHUB_TOKEN_1
    manisharai21 -> GITHUB_TOKEN_2
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_BASE_URL: str = "https://api.github.com"
API_VERSION_HEADER: str = "2022-11-28"

#: Default mapping of GitHub login -> environment variable holding its token.
DEFAULT_USER_TOKEN_ENV: dict[str, str] = {
    "manisharai01": "GITHUB_TOKEN_1",
    "manisharai21": "GITHUB_TOKEN_2",
}

DEFAULT_OUTPUT_DIR: Path = Path("output")
DEFAULT_CONCURRENCY: int = 8
DEFAULT_PER_PAGE: int = 100
DEFAULT_MAX_RETRIES: int = 5
DEFAULT_REQUEST_TIMEOUT: float = 60.0


class ConfigError(RuntimeError):
    """Raised when the application cannot be configured (e.g. missing token)."""


@dataclass(slots=True)
class Account:
    """An authenticated GitHub account used to access the API."""

    login: str
    token: str
    token_env: str

    def masked_token(self) -> str:
        """A safe-to-log representation of the token."""
        if not self.token:
            return "<empty>"
        if len(self.token) <= 8:
            return "****"
        return f"{self.token[:4]}…{self.token[-4:]}"


@dataclass(slots=True)
class AppConfig:
    """Fully resolved application configuration."""

    accounts: list[Account]
    target_logins: list[str]
    output_dir: Path = DEFAULT_OUTPUT_DIR
    concurrency: int = DEFAULT_CONCURRENCY
    per_page: int = DEFAULT_PER_PAGE
    max_retries: int = DEFAULT_MAX_RETRIES
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    scan_all_branches: bool = True  # scan every branch by default (complete coverage)
    skip_forks: bool = False
    collect_commits: bool = True
    collect_prs: bool = True
    make_charts: bool = True
    max_repos: int | None = None
    log_level: str = "INFO"
    # Discovery augmentation
    use_search_discovery: bool = True
    enumerate_org_repos: bool = True
    extra_repos: list[str] = field(default_factory=list)
    extra_orgs: list[str] = field(default_factory=list)
    user_token_env: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_USER_TOKEN_ENV))

    @property
    def charts_dir(self) -> Path:
        return self.output_dir / "charts"

    @property
    def log_file(self) -> Path:
        return self.output_dir / "run.log"


def _maybe_load_dotenv() -> None:
    """Populate ``os.environ`` from a local ``.env`` file if python-dotenv is
    installed.  This is entirely optional - real environment variables always
    take precedence and the tool works fine without the package."""
    try:
        from dotenv import load_dotenv  # type: ignore import-not-found
    except Exception:  # pragma: no cover - dotenv is an optional dependency
        return
    load_dotenv(override=False)


def _sanitize_login_for_env(login: str) -> str:
    """Turn a GitHub login into an environment-variable-safe suffix."""
    return re.sub(r"[^A-Za-z0-9]", "_", login).upper()


def token_env_candidates(login: str, mapping: dict[str, str]) -> list[str]:
    """Ordered list of environment variable names that may hold ``login``'s token.

    Resolution order (first one that is set wins):
      1. An explicit mapping entry (e.g. manisharai01 -> GITHUB_TOKEN_1).
      2. ``GITHUB_TOKEN_<SANITIZED_LOGIN>`` (e.g. octocat -> GITHUB_TOKEN_OCTOCAT).
      3. ``GITHUB_TOKEN`` (single-user convenience).

    This lets the tool work for *any* GitHub user, not just the built-in two.
    """
    candidates: list[str] = []
    if login in mapping:
        candidates.append(mapping[login])
    candidates.append(f"GITHUB_TOKEN_{_sanitize_login_for_env(login)}")
    candidates.append("GITHUB_TOKEN")
    seen: set[str] = set()
    ordered: list[str] = []
    for name in candidates:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def load_accounts(
    selected_logins: list[str],
    user_token_env: dict[str, str] | None = None,
) -> list[Account]:
    """Resolve the tokens for ``selected_logins`` from the environment.

    Raises :class:`ConfigError` if a required token is missing or empty.
    """
    _maybe_load_dotenv()
    mapping = user_token_env or DEFAULT_USER_TOKEN_ENV
    accounts: list[Account] = []
    missing: list[str] = []
    for login in selected_logins:
        candidates = token_env_candidates(login, mapping)
        chosen_env: str | None = None
        token = ""
        for env_name in candidates:
            value = (os.environ.get(env_name) or "").strip()
            if value:
                chosen_env, token = env_name, value
                break
        if not token:
            missing.append(f"{login} (looked in: {', '.join('$' + c for c in candidates)})")
            continue
        accounts.append(Account(login=login, token=token, token_env=chosen_env or candidates[0]))
    if missing:
        raise ConfigError(
            "Missing GitHub token(s) for: "
            + "; ".join(missing)
            + ".\nSet a token as an environment variable or in a .env file. "
            "See .env.example for the expected names."
        )
    if not accounts:
        raise ConfigError("No accounts could be configured - nothing to do.")
    return accounts


def _parse_env_list(name: str) -> list[str]:
    """Parse a comma/whitespace-separated environment variable into a list."""
    raw = os.environ.get(name) or ""
    parts = [p.strip() for chunk in raw.split(",") for p in chunk.split()]
    return [p for p in parts if p]


def build_config(
    *,
    selected_logins: list[str],
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    concurrency: int = DEFAULT_CONCURRENCY,
    scan_all_branches: bool = True,
    skip_forks: bool = False,
    collect_commits: bool = True,
    collect_prs: bool = True,
    make_charts: bool = True,
    max_repos: int | None = None,
    log_level: str = "INFO",
    use_search_discovery: bool = True,
    enumerate_org_repos: bool = True,
    extra_repos: list[str] | None = None,
    extra_orgs: list[str] | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    user_token_env: dict[str, str] | None = None,
) -> AppConfig:
    """Build a fully validated :class:`AppConfig`.

    ``extra_repos`` / ``extra_orgs`` from the caller are unioned with the
    ``EXTRA_REPOS`` / ``EXTRA_ORGS`` environment variables (loaded from .env).
    """
    mapping = user_token_env or dict(DEFAULT_USER_TOKEN_ENV)
    accounts = load_accounts(selected_logins, mapping)  # also loads .env

    repos = list(dict.fromkeys([*(extra_repos or []), *_parse_env_list("EXTRA_REPOS")]))
    orgs = list(dict.fromkeys([*(extra_orgs or []), *_parse_env_list("EXTRA_ORGS")]))

    return AppConfig(
        accounts=accounts,
        target_logins=list(selected_logins),
        output_dir=Path(output_dir),
        concurrency=max(1, concurrency),
        scan_all_branches=scan_all_branches,
        skip_forks=skip_forks,
        collect_commits=collect_commits,
        collect_prs=collect_prs,
        make_charts=make_charts,
        max_repos=max_repos,
        log_level=log_level,
        use_search_discovery=use_search_discovery,
        enumerate_org_repos=enumerate_org_repos,
        extra_repos=repos,
        extra_orgs=orgs,
        max_retries=max_retries,
        request_timeout=request_timeout,
        user_token_env=mapping,
    )
