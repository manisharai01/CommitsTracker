"""Rebuild :class:`CollectedData` from the CSVs of a previous run.

This powers ``--regen``: every report artifact (HTML, Markdown, Excel, charts,
PDF) can be regenerated from ``commits.csv`` / ``pull_requests.csv`` /
``repositories.csv`` / ``organizations.csv`` without any network access or
GitHub token.  Useful for tweaking report options (e.g. exclusions) after an
expensive collection run.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .exporters import FORMULA_PREFIXES
from .logging_config import get_logger
from .models import (
    CollectedData,
    CommitRecord,
    OrgRecord,
    PullRequestRecord,
    RepoRecord,
    parse_github_datetime,
)

log = get_logger("offline")


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        log.warning("%s not found — continuing without it.", path.name)
        return []
    # utf-8-sig transparently strips the BOM that export_csvs writes.
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _s(row: dict[str, str], key: str) -> str:
    text = (row.get(key) or "").strip()
    # Undo the quote the exporters put before text that a spreadsheet would
    # run as a formula (see exporters.neutralize_formulas).
    if len(text) > 1 and text[0] == "'" and text[1] in FORMULA_PREFIXES:
        return text[1:]
    return text


def _i(row: dict[str, str], key: str) -> int:
    text = _s(row, key)
    try:
        return int(float(text)) if text else 0
    except ValueError:
        return 0


def _b(row: dict[str, str], key: str) -> bool:
    return _s(row, key).lower() in {"true", "1", "yes"}


def _commit(row: dict[str, str]) -> CommitRecord:
    return CommitRecord(
        repository=_s(row, "repository"),
        full_name=_s(row, "full_name"),
        owner=_s(row, "owner"),
        organization=_s(row, "organization"),
        sha=_s(row, "sha"),
        message=_s(row, "message"),
        message_first_line=_s(row, "message_first_line"),
        author_login=_s(row, "author_login"),
        author_name=_s(row, "author_name"),
        author_email=_s(row, "author_email"),
        committer_name=_s(row, "committer_name"),
        committer_email=_s(row, "committer_email"),
        authored_date=parse_github_datetime(_s(row, "authored_date")),
        committed_date=parse_github_datetime(_s(row, "committed_date")),
        branch=_s(row, "branch"),
        url=_s(row, "url"),
        additions=_i(row, "additions"),
        deletions=_i(row, "deletions"),
        files_changed=_i(row, "files_changed"),
        # Older runs did not record these: stats count as fetched when present.
        stats_fetched=_b(row, "stats_fetched") or bool(_i(row, "additions") or _i(row, "deletions")),
        parent_count=_i(row, "parent_count"),
    )


def _pr(row: dict[str, str]) -> PullRequestRecord:
    return PullRequestRecord(
        repository=_s(row, "repository"),
        full_name=_s(row, "full_name"),
        organization=_s(row, "organization"),
        number=_i(row, "number"),
        title=_s(row, "title"),
        author_login=_s(row, "author_login"),
        state=_s(row, "state"),
        merged=_b(row, "merged"),
        created_at=parse_github_datetime(_s(row, "created_at")),
        updated_at=parse_github_datetime(_s(row, "updated_at")),
        closed_at=parse_github_datetime(_s(row, "closed_at")),
        merged_at=parse_github_datetime(_s(row, "merged_at")),
        base_branch=_s(row, "base_branch"),
        head_branch=_s(row, "head_branch"),
        url=_s(row, "url"),
        merge_commit_sha=_s(row, "merge_commit_sha"),
        commit_shas=_s(row, "commit_shas").split(),
    )


def _repo(row: dict[str, str]) -> RepoRecord:
    discovered = {p for p in _s(row, "discovered_via").split(",") if p}
    return RepoRecord(
        full_name=_s(row, "full_name"),
        name=_s(row, "name"),
        owner=_s(row, "owner"),
        organization=_s(row, "organization"),
        is_private=_b(row, "is_private"),
        is_fork=_b(row, "is_fork"),
        is_archived=_b(row, "is_archived"),
        default_branch=_s(row, "default_branch") or "main",
        html_url=_s(row, "html_url"),
        description=_s(row, "description"),
        language=_s(row, "language"),
        stargazers=_i(row, "stargazers"),
        forks=_i(row, "forks"),
        pushed_at=parse_github_datetime(_s(row, "pushed_at")),
        created_at=parse_github_datetime(_s(row, "created_at")),
        discovered_via=discovered,
        # Older runs scanned every repository fully.
        affiliated=_s(row, "affiliated").lower() != "false",
    )


def _org(row: dict[str, str]) -> OrgRecord:
    return OrgRecord(
        login=_s(row, "login"),
        name=_s(row, "name"),
        url=_s(row, "url"),
        is_member=_b(row, "is_member"),
        repos_contributed=_i(row, "repos_contributed"),
        repo_names=[p for p in _s(row, "repo_names").split(",") if p],
        commit_count=_i(row, "commit_count"),
        pr_count=_i(row, "pr_count"),
        merged_pr_count=_i(row, "merged_pr_count"),
    )


def _load_collection_file(output_dir: Path) -> tuple[dict[str, Any], list[str]]:
    """Collection metadata and completeness notes saved by the original run."""
    path = output_dir / "collection.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, []
    except (OSError, ValueError) as exc:
        log.warning("%s is unreadable (%s) — continuing without it.", path.name, exc)
        return {}, []
    meta = payload.get("meta") if isinstance(payload, dict) else None
    notes = payload.get("notes") if isinstance(payload, dict) else None
    return (
        meta if isinstance(meta, dict) else {},
        [str(n) for n in notes] if isinstance(notes, list) else [],
    )


def load_collected_from_csv(output_dir: Path) -> CollectedData:
    """Load a previous run's CSVs back into a :class:`CollectedData`."""
    mappers: list[tuple[str, Any]] = [
        ("commits.csv", _commit),
        ("pull_requests.csv", _pr),
        ("repositories.csv", _repo),
        ("organizations.csv", _org),
    ]
    loaded: dict[str, list[Any]] = {}
    for filename, mapper in mappers:
        rows = _read_rows(output_dir / filename)
        loaded[filename] = [mapper(row) for row in rows]
        log.info("loaded %d row(s) from %s", len(rows), filename)

    meta, notes = _load_collection_file(output_dir)
    data = CollectedData(
        repos=loaded["repositories.csv"],
        commits=loaded["commits.csv"],
        pull_requests=loaded["pull_requests.csv"],
        organizations=loaded["organizations.csv"],
        notes=notes,
        meta=meta,
    )
    if not data.commits and not data.pull_requests:
        log.warning(
            "No commits or pull requests found in %s — run a collection first.",
            output_dir,
        )
    return data
