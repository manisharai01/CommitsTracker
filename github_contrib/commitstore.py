"""Commits on disk, so memory never depends on how many there are.

A report can hold any number of commits: an account with a million of them
must not need a million commits' worth of memory (about 3 KB each once the
report is built). :class:`CommitStore` keeps them in a SQLite file instead,
in the order they were added, and hands them out as :class:`CommitRecord`
objects one at a time while a caller iterates. Everything that summarises
commits does so in streaming passes whose state grows with repositories,
days or months - never with commits.

It behaves like the list of commits it replaces for reading: ``len()``,
truthiness and iteration (in insertion order). It does not support indexing
or slicing; code that needs an order asks for it (:func:`chronological`,
:func:`repo_commits`). The report's own steps - leaving out excluded owners,
the period, counting each change once - mark rows instead of copying lists
(see :meth:`CommitStore.reset` and the ``drop_*`` methods), with the exact
rules of :mod:`filters`.

A plain ``list`` of commits works everywhere a store does (tests, small
data): the helpers here accept either.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

from .models import CommitRecord, PullRequestRecord, RepoRecord

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_US = timedelta(microseconds=1)
_SEC = timedelta(seconds=1)
#: Rows read from SQLite per fetch while iterating.
_FETCH = 2000

#: Why a row is left out of the report (0 = it is in it).
KEPT, OWNER, PERIOD, SAME_SHA, REBASED, SQUASHED = 0, 1, 2, 3, 4, 5

_FIELDS = (
    "repository", "full_name", "owner", "organization", "sha", "message", "message_first_line",
    "author_login", "author_name", "author_email", "committer_name", "committer_email",
    "authored", "committed", "branch", "url", "additions", "deletions", "files_changed",
    "stats_fetched", "parent_count",
)
_SCHEMA = f"""
create table commits (
    seq integer primary key,
    {", ".join(f"{name} {'integer' if name in ('additions', 'deletions', 'files_changed', 'stats_fetched', 'parent_count') else 'text'}" for name in _FIELDS)},
    authored_us integer,      -- instant, microseconds since 1970 (null: no date)
    committed_us integer,
    authored_sec integer,     -- whole seconds (rebased copies keep them)
    ident_author text,        -- lower-cased author email, else login
    ident_subject text,       -- first line, stripped
    owner_key text,           -- lower-cased owner login
    priority integer not null default 0,  -- attribution order during collection
    dropped integer not null default 0
);
create index commits_repo_sha on commits (full_name, sha);
create index commits_sha on commits (sha);
"""


def _us(moment: datetime | None) -> int | None:
    return None if moment is None else (moment - _EPOCH) // _US


def _owner_key(full_name: str, owner: str) -> str:
    """Same as filters._owner_of."""
    if owner:
        return owner.lower()
    return full_name.split("/", 1)[0].lower() if "/" in full_name else full_name.lower()


class CommitStore:
    """A report's commits in a SQLite file (deleted when the store closes).

    ``tz``, once set, is the zone every date is read in (the report's time
    zone): the stored instants never change.
    """

    def __init__(self, directory: Path | str | None = None) -> None:
        fd, name = tempfile.mkstemp(prefix="commits-", suffix=".sqlite", dir=directory)
        os.close(fd)
        self.path = Path(name)
        self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        for pragma in (
            "journal_mode = OFF", "synchronous = OFF", "temp_store = FILE",
            "cache_size = -16000",  # 16 MB of page cache, however big the file
        ):
            self._db.execute(f"pragma {pragma}")
        self._db.executescript(_SCHEMA)
        self.tz: tzinfo | None = None
        self._count: int | None = None

    # -- life cycle ---------------------------------------------------------

    def close(self) -> None:
        try:
            self._db.close()
        finally:
            self.path.unlink(missing_ok=True)

    def __enter__(self) -> CommitStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:  # a store dropped without close() still cleans up
        try:
            if self.path.exists():
                self.close()
        except Exception:  # noqa: BLE001 - interpreter shutting down
            pass

    @classmethod
    def from_records(cls, records: Iterable[CommitRecord], directory: Path | str | None = None) -> CommitStore:
        store = cls(directory)
        store.add(records)
        return store

    # -- writing ------------------------------------------------------------

    def add(self, records: Iterable[CommitRecord], priority: int = 0) -> int:
        """Append ``records`` (in order); returns how many."""
        added = 0
        batch: list[tuple] = []
        insert = (
            f"insert into commits ({', '.join(_FIELDS)}, authored_us, committed_us, authored_sec, "
            f"ident_author, ident_subject, owner_key, priority) values ({', '.join('?' * (len(_FIELDS) + 7))})"
        )
        for c in records:
            batch.append(self._row(c, priority))
            if len(batch) >= _FETCH:
                self._db.executemany(insert, batch)
                added += len(batch)
                batch.clear()
        if batch:
            self._db.executemany(insert, batch)
            added += len(batch)
        self._count = None
        return added

    @staticmethod
    def _row(c: CommitRecord, priority: int) -> tuple:
        authored = c.authored_date
        return (
            c.repository, c.full_name, c.owner, c.organization, c.sha, c.message, c.message_first_line,
            c.author_login, c.author_name, c.author_email, c.committer_name, c.committer_email,
            authored.isoformat() if authored else None,
            c.committed_date.isoformat() if c.committed_date else None,
            c.branch, c.url, c.additions, c.deletions, c.files_changed, int(c.stats_fetched), c.parent_count,
            _us(authored), _us(c.committed_date),
            (authored - _EPOCH) // _SEC if authored else None,
            (c.author_email or c.author_login).lower(),
            c.message_first_line.strip(),
            _owner_key(c.full_name, c.owner),
            priority,
        )

    def set_line_stats(self, updates: Iterable[tuple[int, int, int, int]]) -> None:
        """Line statistics for many rows: ``(seq, additions, deletions, files)``."""
        self._db.executemany(
            "update commits set additions = ?, deletions = ?, files_changed = ?, stats_fetched = 1 where seq = ?",
            [(a, d, f, seq) for seq, a, d, f in updates],
        )

    def delete_repo(self, full_name: str, priority: int | None = None) -> None:
        """Forget a repository's rows (those of one ``priority``, when given)."""
        if priority is None:
            self._db.execute("delete from commits where full_name = ?", (full_name,))
        else:
            self._db.execute("delete from commits where full_name = ? and priority = ?", (full_name, priority))
        self._count = None

    def keep_first_per_repo_sha(self) -> int:
        """Collection: one row per (repository, SHA) - the lowest priority,
        then the first added (what the collectors' order meant)."""
        cur = self._db.execute(
            "delete from commits where seq in ("
            " select seq from (select seq, row_number() over ("
            "   partition by full_name, sha order by priority, seq) as rn"
            "  from commits where sha != '') where rn > 1)"
        )
        self._count = None
        return cur.rowcount

    # -- reading ------------------------------------------------------------

    def __len__(self) -> int:
        if self._count is None:
            self._count = self._db.execute("select count(*) from commits where dropped = 0").fetchone()[0]
        return self._count

    def __bool__(self) -> bool:
        return len(self) > 0

    def __iter__(self) -> Iterator[CommitRecord]:
        return self.records()

    def records(self, where: str = "", params: tuple = (), order: str = "seq") -> Iterator[CommitRecord]:
        """Kept rows as records (``where``: an extra SQL condition)."""
        for _seq, record in self.rows(where, params, order):
            yield record

    def rows(self, where: str = "", params: tuple = (), order: str = "seq") -> Iterator[tuple[int, CommitRecord]]:
        """``(seq, record)`` of kept rows, in ``order``."""
        sql = f"select seq, {', '.join(_FIELDS)} from commits where dropped = 0"
        if where:
            sql += f" and ({where})"
        cur = self._db.execute(f"{sql} order by {order}", params)
        tz = self.tz
        while True:
            chunk = cur.fetchmany(_FETCH)
            if not chunk:
                return
            for row in chunk:
                yield row[0], self._record(row, tz)

    @staticmethod
    def _record(row: tuple, tz: tzinfo | None) -> CommitRecord:
        (_seq, repository, full_name, owner, organization, sha, message, first_line, author_login, author_name,
         author_email, committer_name, committer_email, authored, committed, branch, url, additions, deletions,
         files_changed, stats_fetched, parent_count) = row
        a = datetime.fromisoformat(authored) if authored else None
        c = datetime.fromisoformat(committed) if committed else None
        if tz is not None:
            a = a.astimezone(tz) if a is not None else None
            c = c.astimezone(tz) if c is not None else None
        return CommitRecord(
            repository=repository, full_name=full_name, owner=owner, organization=organization, sha=sha,
            message=message, message_first_line=first_line, author_login=author_login, author_name=author_name,
            author_email=author_email, committer_name=committer_name, committer_email=committer_email,
            authored_date=a, committed_date=c, branch=branch, url=url, additions=additions, deletions=deletions,
            files_changed=files_changed, stats_fetched=bool(stats_fetched), parent_count=parent_count,
        )

    def has(self, full_name: str, sha: str) -> bool:
        return self._db.execute(
            "select 1 from commits where full_name = ? and sha = ? and dropped = 0 limit 1", (full_name, sha)
        ).fetchone() is not None

    def repositories(self) -> set[str]:
        return {name for (name,) in self._db.execute("select distinct full_name from commits where dropped = 0")}

    # -- the report's steps (see filters.prepare_report_data) ---------------

    def reset(self) -> None:
        """Every row back in the report, dates as stored."""
        self._db.execute("update commits set dropped = 0 where dropped != 0")
        self.tz = None
        self._count = None

    def _drop(self, reason: int, where: str, params: tuple = ()) -> int:
        cur = self._db.execute(f"update commits set dropped = ? where dropped = 0 and ({where})", (reason, *params))
        self._count = None
        return cur.rowcount

    def drop_owners(self, owners: set[str]) -> int:
        if not owners:
            return 0
        marks = ", ".join("?" * len(owners))
        return self._drop(OWNER, f"owner_key in ({marks})", tuple(sorted(owners)))

    def drop_outside(self, since: datetime | None, until: datetime | None) -> int:
        """Rows authored outside ``[since, until]`` (and undated ones)."""
        if since is None and until is None:
            return 0
        where = ["authored_us is null"]
        params: list[int] = []
        if since is not None:
            where.append("authored_us < ?")
            params.append(_us(since))
        if until is not None:
            where.append("authored_us > ?")
            params.append(_us(until))
        return self._drop(PERIOD, " or ".join(where), tuple(params))

    def _load_repos(self, repos: Iterable[RepoRecord]) -> None:
        self._db.execute("drop table if exists temp.repo_info")
        self._db.execute("create temp table repo_info (full_name text primary key, is_fork integer, default_branch text)")
        self._db.executemany(
            "insert or replace into temp.repo_info values (?, ?, ?)",
            [(r.full_name, int(r.is_fork), r.default_branch) for r in repos],
        )

    # filters.dedupe_contributions's preference: a copy in a non-fork
    # repository, on its default branch, committed first; then by name and
    # SHA; then the first added.
    _PREFERENCE = (
        "coalesce(r.is_fork, 0), "
        "case when r.full_name is not null and c.branch = r.default_branch then 0 else 1 end, "
        "c.committed_us is null, c.committed_us, c.full_name, c.sha, c.seq"
    )

    def drop_same_sha(self, repos: Iterable[RepoRecord]) -> int:
        """One SHA in several repositories (a fork and its upstream): keep one."""
        self._load_repos(repos)
        return self._drop(SAME_SHA, (
            "seq in (select seq from (select c.seq, row_number() over ("
            f" partition by c.sha order by {self._PREFERENCE}) as rn"
            " from commits c left join temp.repo_info r on r.full_name = c.full_name"
            " where c.dropped = 0 and c.sha != '') where rn > 1)"
        ))

    def drop_rebased(self, repos: Iterable[RepoRecord]) -> int:
        """Rebased / cherry-picked copies: same repository, author, author
        second and subject."""
        self._load_repos(repos)
        return self._drop(REBASED, (
            "seq in (select seq from (select c.seq, row_number() over ("
            " partition by c.full_name, c.ident_author, c.authored_sec, c.ident_subject"
            f" order by {self._PREFERENCE}) as rn"
            " from commits c left join temp.repo_info r on r.full_name = c.full_name"
            " where c.dropped = 0 and c.authored_sec is not null) where rn > 1)"
        ))

    def drop_squashed(self, prs: Iterable[PullRequestRecord]) -> int:
        """Original commits of squash-merged pull requests whose squash commit
        is in the report (filters.dedupe_contributions, step 3)."""
        folded: list[tuple[str, str]] = []
        for pr in prs:
            if not pr.merge_commit_sha or not pr.commit_shas or pr.merge_commit_sha in pr.commit_shas:
                continue
            squash = self._db.execute(
                "select parent_count from commits where dropped = 0 and full_name = ? and sha = ? order by seq limit 1",
                (pr.full_name, pr.merge_commit_sha),
            ).fetchone()
            if squash is None or squash[0] != 1:
                continue
            folded += [(pr.full_name, sha) for sha in pr.commit_shas]
        if not folded:
            return 0
        self._db.execute("drop table if exists temp.folded")
        self._db.execute("create temp table folded (full_name text, sha text)")
        self._db.executemany("insert into temp.folded values (?, ?)", folded)
        return self._drop(SQUASHED, (
            "exists (select 1 from temp.folded f where f.full_name = commits.full_name and f.sha = commits.sha)"
        ))


# ---------------------------------------------------------------------------
# Either a store or a list
# ---------------------------------------------------------------------------


def chronological(commits: Iterable[CommitRecord], skip: int = 0) -> Iterator[CommitRecord]:
    """Oldest first (undated last), ties in the original order; without the
    first ``skip``."""
    if isinstance(commits, CommitStore):
        order = "authored_us is null, authored_us, seq"
        if skip:
            order += f" limit -1 offset {int(skip)}"
        return commits.records(order=order)
    ordered = sorted(commits, key=lambda c: (c.authored_date is None, _us(c.authored_date) or 0))
    return iter(ordered[skip:])


def repo_commits(commits: Iterable[CommitRecord], full_name: str, limit: int | None = None) -> Iterator[CommitRecord]:
    """One repository's commits, newest first (undated last)."""
    if isinstance(commits, CommitStore):
        order = "authored_us is null, authored_us desc, seq"
        if limit is not None:
            order += f" limit {int(limit)}"
        return commits.records("full_name = ?", (full_name,), order)
    mine = [c for c in commits if c.full_name == full_name]
    mine.sort(key=lambda c: (c.authored_date is None, -(_us(c.authored_date) or 0)))
    return iter(mine[:limit] if limit is not None else mine)

