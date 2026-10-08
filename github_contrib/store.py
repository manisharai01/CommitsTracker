"""Report history for the hosted web app (GitHub sign-in mode).

The database holds the bare minimum, on purpose (see ``schema.sql``):

* ``users``   — the GitHub numeric user id and when it first signed in;
* ``reports`` — id (= the job id), owner, created_at, period, status.

No tokens, logins, emails, report files, summaries or options are stored.

:class:`PostgresStore` talks to Postgres (e.g. Supabase) through asyncpg;
:class:`MemoryStore` keeps the same data in memory, for tests and for running
without ``DATABASE_URL`` (history is then lost on restart).

Callers treat every store error as non-fatal: a report run or a page load must
never fail because the history could not be written or read.
"""

from __future__ import annotations

import abc
import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .logging_config import get_logger

log = get_logger("store")

#: Idempotent schema, run at startup (and shipped as schema.sql).
#: Row level security is enabled with no policies: Supabase exposes the public
#: schema through its REST API, and RLS without policies denies every request
#: made that way. The server connects as the postgres role, which bypasses RLS.
SCHEMA_SQL = """
create table if not exists users (
    id         bigint primary key,
    created_at timestamptz not null default now()
);

create table if not exists reports (
    id         uuid primary key,
    user_id    bigint not null references users(id) on delete cascade,
    created_at timestamptz not null default now(),
    period     text not null,
    status     text not null
);

create index if not exists reports_user_created_idx on reports (user_id, created_at desc);

alter table users enable row level security;
alter table reports enable row level security;
"""

UNFINISHED = ("queued", "running")


class StoreError(RuntimeError):
    """The history database is unavailable."""


@dataclass(frozen=True, slots=True)
class ReportRow:
    id: str  # canonical uuid text, same as the job id
    user_id: int
    created_at: datetime
    period: str  # "<since>..<until>"; ".." = all time
    status: str


def _uuid(report_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(report_id))
    except (ValueError, TypeError, AttributeError):
        return None


class Store(abc.ABC):
    """What the web app needs from the history database."""

    #: Whether the history survives a restart.
    persistent: bool = False

    async def start(self) -> None:  # noqa: B027 - optional hook
        """Connect and create the schema (failures are logged, not raised)."""

    async def close(self) -> None:  # noqa: B027 - optional hook
        """Release connections."""

    @abc.abstractmethod
    async def ensure_user(self, user_id: int) -> None: ...

    @abc.abstractmethod
    async def add_report(self, report_id: str, user_id: int, period: str, status: str) -> None: ...

    @abc.abstractmethod
    async def set_status(self, report_id: str, status: str) -> None: ...

    @abc.abstractmethod
    async def list_reports(self, user_id: int, limit: int = 100) -> list[ReportRow]: ...

    @abc.abstractmethod
    async def delete_report(self, report_id: str, user_id: int) -> bool:
        """Delete one of ``user_id``'s reports; whether a row was deleted."""

    @abc.abstractmethod
    async def fail_unfinished(self) -> int:
        """Mark queued/running rows as failed (their runs died with the last server)."""


class MemoryStore(Store):
    """The history in a dict: for tests, and when no DATABASE_URL is set."""

    def __init__(self) -> None:
        self.users: dict[int, datetime] = {}
        self.reports: dict[str, ReportRow] = {}

    async def ensure_user(self, user_id: int) -> None:
        self.users.setdefault(int(user_id), datetime.now(timezone.utc))

    async def add_report(self, report_id: str, user_id: int, period: str, status: str) -> None:
        key = _uuid(report_id)
        if key is None:
            raise StoreError(f"not a report id: {report_id!r}")
        await self.ensure_user(user_id)
        self.reports.setdefault(
            str(key), ReportRow(str(key), int(user_id), datetime.now(timezone.utc), period, status)
        )

    async def set_status(self, report_id: str, status: str) -> None:
        key = str(_uuid(report_id))
        row = self.reports.get(key)
        if row is not None:
            self.reports[key] = ReportRow(row.id, row.user_id, row.created_at, row.period, status)

    async def list_reports(self, user_id: int, limit: int = 100) -> list[ReportRow]:
        rows = [row for row in self.reports.values() if row.user_id == int(user_id)]
        rows.sort(key=lambda row: row.created_at, reverse=True)
        return rows[: max(0, limit)]

    async def delete_report(self, report_id: str, user_id: int) -> bool:
        key = str(_uuid(report_id))
        row = self.reports.get(key)
        if row is None or row.user_id != int(user_id):
            return False
        del self.reports[key]
        return True

    async def fail_unfinished(self) -> int:
        stale = [row for row in self.reports.values() if row.status in UNFINISHED]
        for row in stale:
            await self.set_status(row.id, "failed")
        return len(stale)


class PostgresStore(Store):
    """The history in Postgres, through a small asyncpg pool.

    ``statement_cache_size=0`` keeps asyncpg from naming prepared statements,
    which Supabase's connection poolers (Supavisor / PgBouncer) need. When the
    database can't be reached the pool is retried at most every
    ``retry_seconds``; until then every call raises :class:`StoreError`.
    """

    persistent = True

    def __init__(
        self, dsn: str, *, min_size: int = 1, max_size: int = 3, retry_seconds: float = 30.0
    ) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._retry_seconds = retry_seconds
        self._pool = None
        self._lock: asyncio.Lock | None = None
        self._next_attempt = 0.0

    def __repr__(self) -> str:  # never show the DSN: it holds the password
        return f"PostgresStore(connected={self._pool is not None})"

    async def _connect(self):
        if self._pool is not None:
            return self._pool
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._pool is not None:
                return self._pool
            if time.monotonic() < self._next_attempt:
                raise StoreError("the history database is unavailable")
            try:
                import asyncpg  # imported here so local mode never needs it

                pool = await asyncpg.create_pool(
                    self._dsn,
                    min_size=self._min_size,
                    max_size=self._max_size,
                    statement_cache_size=0,
                    command_timeout=10,
                    timeout=10,
                )
                try:
                    async with pool.acquire() as conn:
                        await conn.execute(SCHEMA_SQL)
                except BaseException:
                    await pool.close()
                    raise
            except Exception as exc:
                self._next_attempt = time.monotonic() + self._retry_seconds
                reason = _describe(exc, self._dsn)
                raise StoreError(f"could not connect to the history database ({reason})") from None
            self._pool = pool
            log.info("report history: connected to the database")
            return pool

    async def start(self) -> None:
        try:
            await self._connect()
        except StoreError as exc:
            log.warning("report history: %s; will retry", exc)

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            try:
                await asyncio.wait_for(pool.close(), timeout=10)
            except Exception:  # noqa: BLE001 - shutting down anyway
                pool.terminate()

    async def ensure_user(self, user_id: int) -> None:
        pool = await self._connect()
        await pool.execute(
            "insert into users (id) values ($1) on conflict (id) do nothing", int(user_id)
        )

    async def add_report(self, report_id: str, user_id: int, period: str, status: str) -> None:
        key = _uuid(report_id)
        if key is None:
            raise StoreError(f"not a report id: {report_id!r}")
        pool = await self._connect()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "insert into users (id) values ($1) on conflict (id) do nothing", int(user_id)
                )
                await conn.execute(
                    "insert into reports (id, user_id, period, status) values ($1, $2, $3, $4) "
                    "on conflict (id) do nothing",
                    key, int(user_id), period, status,
                )

    async def set_status(self, report_id: str, status: str) -> None:
        key = _uuid(report_id)
        if key is None:
            return
        pool = await self._connect()
        await pool.execute("update reports set status = $2 where id = $1", key, status)

    async def list_reports(self, user_id: int, limit: int = 100) -> list[ReportRow]:
        pool = await self._connect()
        rows = await pool.fetch(
            "select id, user_id, created_at, period, status from reports "
            "where user_id = $1 order by created_at desc limit $2",
            int(user_id), max(0, int(limit)),
        )
        return [
            ReportRow(str(r["id"]), int(r["user_id"]), r["created_at"], r["period"], r["status"])
            for r in rows
        ]

    async def delete_report(self, report_id: str, user_id: int) -> bool:
        key = _uuid(report_id)
        if key is None:
            return False
        pool = await self._connect()
        result = await pool.execute(
            "delete from reports where id = $1 and user_id = $2", key, int(user_id)
        )
        return result.split()[-1] != "0"  # "DELETE <count>"

    async def fail_unfinished(self) -> int:
        pool = await self._connect()
        result = await pool.execute(
            "update reports set status = 'failed' where status = any($1::text[])", list(UNFINISHED)
        )
        try:
            return int(result.split()[-1])  # "UPDATE <count>"
        except (ValueError, IndexError):
            return 0


def _describe(exc: BaseException, dsn: str) -> str:
    """A short description of a connection error, with no part of the DSN's
    credentials in it (a mis-encoded password can end up in a parse error)."""
    name = exc.__class__.__name__
    if isinstance(exc, ValueError):  # includes asyncpg's ClientConfigurationError
        return f"{name}: DATABASE_URL could not be parsed; URL-encode special characters in the password"
    text = (str(exc).strip().splitlines() or [""])[0][:200]
    rest = dsn.split("://", 1)[-1]
    userinfo = rest.rsplit("@", 1)[0] if "@" in rest else ""
    secrets_ = {userinfo, userinfo.partition(":")[2], dsn}
    try:
        secrets_.add(urlsplit(dsn).password or "")
    except ValueError:
        pass
    for secret in sorted(secrets_, key=len, reverse=True):
        if len(secret) >= 4:
            text = text.replace(secret, "***")
    return f"{name}: {text}" if text else name


def make_store(database_url: str) -> Store:
    """PostgresStore for ``database_url``; MemoryStore (with a warning) without one."""
    if database_url.strip():
        return PostgresStore(database_url.strip())
    log.warning(
        "DATABASE_URL is not set: report history is kept in memory and is lost "
        "when the server restarts."
    )
    return MemoryStore()
