"""Code reviews the tracked accounts gave on other people's pull requests.

Read from GitHub's contribution data (``contributionsCollection``) with each
account's own token, since only the account itself sees its contributions in
private repositories. GitHub serves at most a year per query, so the period
(or, for all time, everything since the account was created) is read one
year at a time. Reviews of the tracked accounts' own pull requests are left
out: reviewing yourself isn't collaboration.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .client import GitHubClient
from .graphql import _PAGE_INFO, Walk, _Progress, _single, _str, run_walks
from .logging_config import get_logger
from .models import Coverage, ReviewRecord, parse_github_datetime

log = get_logger("reviews")

#: Users or contribution windows per query.
REVIEW_BATCH = 4
#: GitHub's longest contributionsCollection span.
_WINDOW = timedelta(days=365)
_REVIEW_FIELDS = (
    "occurredAt pullRequestReview { state url } "
    "pullRequest { number title url author { login } repository { nameWithOwner owner { login __typename } } }"
)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window_walk(client: GitHubClient, login: str, start: datetime, end: datetime) -> Walk:
    def body(size: int, after: str) -> str:
        return (
            f"contributionsCollection(from: {_str(_iso(start))}, to: {_str(_iso(end))}) {{ "
            f"restrictedContributionsCount pullRequestReviewContributions(first: {size}{after}) "
            f"{{ totalCount {_PAGE_INFO} nodes {{ {_REVIEW_FIELDS} }} }} }}"
        )

    return Walk(
        client, f"user(login: {_str(login)})", body,
        lambda obj: (obj.get("contributionsCollection") or {}).get("pullRequestReviewContributions"),
    )


def review_record(node: dict, reviewer: str) -> ReviewRecord | None:
    """A review contribution as a record (None when GitHub left parts out)."""
    pr = node.get("pullRequest") or {}
    repo = pr.get("repository") or {}
    review = node.get("pullRequestReview") or {}
    full = str(repo.get("nameWithOwner") or "")
    if not full or not isinstance(pr.get("number"), int):
        return None
    owner = repo.get("owner") or {}
    owner_login = str(owner.get("login") or full.split("/", 1)[0])
    return ReviewRecord(
        reviewer=reviewer,
        full_name=full,
        owner=owner_login,
        organization=owner_login if owner.get("__typename") == "Organization" else "",
        pr_number=pr["number"],
        pr_title=str(pr.get("title") or ""),
        pr_author=str((pr.get("author") or {}).get("login") or ""),
        state=str(review.get("state") or ""),
        submitted_at=parse_github_datetime(node.get("occurredAt")),
        url=str(review.get("url") or pr.get("url") or ""),
    )


async def collect_reviews(
    clients: dict[str, GitHubClient],
    logins: list[str],
    *,
    since: datetime | None,
    until: datetime | None,
    coverage: Coverage,
    inflight: int,
    now: datetime | None = None,
) -> list[ReviewRecord]:
    """Every review ``logins`` gave in the period (or ever), each read with the
    account's own client when there is one. Gaps become coverage notes; never
    raises."""
    now = now or datetime.now(timezone.utc)
    tracked = {login.lower() for login in logins}
    fallback = next(iter(clients.values()), None)
    if fallback is None or not logins:
        return []
    own = {login: clients.get(login) or fallback for login in logins}
    try:
        # When each account was created: an all-time report starts there.
        born = {
            login: Walk(client, f"user(login: {_str(login)})", lambda _s, _a: "createdAt", _single)
            for login, client in own.items()
        }
        await run_walks(list(born.values()), REVIEW_BATCH, inflight=inflight)
        windows: list[tuple[str, datetime, datetime, Walk]] = []
        for login, walk in born.items():
            if walk.error or not walk.nodes:
                coverage.warn(f"[{login}] code reviews could not be read ({walk.error or 'no answer'}).")
                continue
            start = parse_github_datetime(walk.nodes[0].get("createdAt")) or now - 10 * _WINDOW
            if since is not None:
                start = max(start, since)
            end = min(until, now) if until is not None else now
            while start < end:
                stop = min(start + _WINDOW, end)
                windows.append((login, start, stop, _window_walk(own[login], login, start, stop)))
                start = stop
        with _Progress("Reviews", len(windows)) as bar:
            await run_walks([w for *_rest, w in windows], REVIEW_BATCH, inflight=inflight, progress=bar)
    except Exception as exc:  # noqa: BLE001 - reviews are a bonus, never fatal
        coverage.warn(f"Code reviews could not be read ({type(exc).__name__}: {exc}).")
        return []

    reviews: list[ReviewRecord] = []
    seen: set[str] = set()
    hidden: dict[str, int] = {}
    for login, start, stop, walk in windows:
        if walk.error:
            coverage.warn(
                f"[{login}] code reviews from {start:%d %b %Y} to {stop:%d %b %Y} could not be read: {walk.error}"
            )
            continue
        collection = (walk.root_data or {}).get("contributionsCollection") or {}
        hidden[login] = hidden.get(login, 0) + int(collection.get("restrictedContributionsCount") or 0)
        for node in walk.nodes:
            record = review_record(node, login)
            if record is None or record.pr_author.lower() in tracked:
                continue  # reviewing one's own pull request isn't a review of others
            key = record.url or f"{record.full_name}#{record.pr_number}@{record.submitted_at}"
            if key not in seen:
                seen.add(key)
                reviews.append(record)
    for login, count in hidden.items():
        if count:
            coverage.warn(
                f"[{login}] {count:,} contribution(s) in private repositories are hidden from this token, so "
                f"code reviews there may be missing. A token created by {login} sees them."
            )
    log.info("collected %d code review(s)", len(reviews))
    return reviews
