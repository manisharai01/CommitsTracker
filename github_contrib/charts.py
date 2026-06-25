"""Chart and dashboard generation with matplotlib.

The Agg (non-interactive) backend is selected before importing pyplot so the
module works in headless environments (CI, servers).
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402  (must follow backend selection)

from .logging_config import get_logger
from .statistics import Statistics

log = get_logger("charts")

_PALETTE = "#305496"


def _save(fig: "plt.Figure", path: Path, tight: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # tight_layout is incompatible with manually managed GridSpec figures
    # (the dashboard), so callers opt out there.
    if tight:
        fig.tight_layout()
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    log.info("wrote chart %s", path.name)
    return path


def _empty_axes(ax: "plt.Axes", title: str) -> None:
    ax.set_title(title)
    ax.text(0.5, 0.5, "no data", ha="center", va="center", transform=ax.transAxes, color="gray")
    ax.set_xticks([])
    ax.set_yticks([])


def chart_commits_per_year(stats: Statistics, path: Path) -> Path:
    fig, ax = plt.subplots(figsize=(9, 5))
    df = stats.commits_per_year
    if df.empty:
        _empty_axes(ax, "Commits per Year")
    else:
        ax.bar(df["year"].astype(str), df["commits"], color=_PALETTE)
        ax.set_title("Commits per Year")
        ax.set_xlabel("Year")
        ax.set_ylabel("Commits")
        ax.grid(axis="y", alpha=0.3)
    return _save(fig, path)


def chart_commits_per_month(stats: Statistics, path: Path) -> Path:
    fig, ax = plt.subplots(figsize=(12, 5))
    df = stats.commits_per_month
    if df.empty:
        _empty_axes(ax, "Commits per Month")
    else:
        ax.plot(df["month"].astype(str), df["commits"], marker="o", color=_PALETTE)
        ax.set_title("Commits per Month")
        ax.set_xlabel("Month")
        ax.set_ylabel("Commits")
        ax.grid(alpha=0.3)
        # Avoid an unreadable axis when there are many months.
        step = max(1, len(df) // 24)
        ax.set_xticks(range(0, len(df), step))
        ax.set_xticklabels(df["month"].astype(str).iloc[::step], rotation=60, ha="right", fontsize=8)
    return _save(fig, path)


def chart_top_repositories(stats: Statistics, path: Path, top_n: int = 15) -> Path:
    fig, ax = plt.subplots(figsize=(10, 6))
    df = stats.commits_per_repo.head(top_n)
    if df.empty:
        _empty_axes(ax, "Top Repositories by Commits")
    else:
        labels = df["full_name"].astype(str)
        ax.barh(labels, df["commits"], color=_PALETTE)
        ax.invert_yaxis()
        ax.set_title(f"Top {len(df)} Repositories by Commits")
        ax.set_xlabel("Commits")
        ax.grid(axis="x", alpha=0.3)
    return _save(fig, path)


def chart_commits_by_weekday(stats: Statistics, path: Path) -> Path:
    fig, ax = plt.subplots(figsize=(9, 5))
    df = stats.commits_by_weekday
    if df.empty or df["commits"].sum() == 0:
        _empty_axes(ax, "Commits by Day of Week")
    else:
        ax.bar(df["weekday"].astype(str).str.slice(0, 3), df["commits"], color=_PALETTE)
        ax.set_title("Commits by Day of Week")
        ax.set_ylabel("Commits")
        ax.grid(axis="y", alpha=0.3)
    return _save(fig, path)


def chart_commits_by_org(stats: Statistics, path: Path) -> Path:
    fig, ax = plt.subplots(figsize=(9, 6))
    df = stats.commits_by_org
    if df.empty or df["commits"].sum() == 0:
        _empty_axes(ax, "Commits by Organization")
    else:
        top = df.head(10)
        ax.barh(top["organization"].astype(str), top["commits"], color=_PALETTE)
        ax.invert_yaxis()
        ax.set_title("Commits by Organization")
        ax.set_xlabel("Commits")
        ax.grid(axis="x", alpha=0.3)
    return _save(fig, path)


def build_dashboard(stats: Statistics, path: Path) -> Path:
    """A single multi-panel PNG summarising the whole report."""
    fig = plt.figure(figsize=(16, 11))
    gs = fig.add_gridspec(2, 2, hspace=0.35, wspace=0.25)

    # Headline text panel.
    ax_text = fig.add_subplot(gs[0, 0])
    ax_text.axis("off")
    s = stats.summary_dict
    headline = "\n".join(
        [
            "GitHub Contribution Dashboard",
            "",
            f"Tracked users: {s.get('tracked_users', '')}",
            f"Lifetime commits: {s.get('total_lifetime_commits', 0)}",
            f"Pull requests: {s.get('total_pull_requests', 0)} "
            f"(merged {s.get('merged_pull_requests', 0)})",
            f"Repositories contributed: {s.get('repositories_contributed_to', 0)}",
            f"Organizations contributed: {s.get('organizations_contributed_to', 0)}",
            f"First contribution: {s.get('first_contribution_date', '') or 'n/a'}",
            f"Latest contribution: {s.get('latest_contribution_date', '') or 'n/a'}",
        ]
    )
    ax_text.text(0.0, 1.0, headline, va="top", ha="left", fontsize=13, family="monospace")

    # Commits per year.
    ax_year = fig.add_subplot(gs[0, 1])
    dfy = stats.commits_per_year
    if dfy.empty:
        _empty_axes(ax_year, "Commits per Year")
    else:
        ax_year.bar(dfy["year"].astype(str), dfy["commits"], color=_PALETTE)
        ax_year.set_title("Commits per Year")
        ax_year.grid(axis="y", alpha=0.3)

    # Commits per month.
    ax_month = fig.add_subplot(gs[1, 0])
    dfm = stats.commits_per_month
    if dfm.empty:
        _empty_axes(ax_month, "Commits per Month")
    else:
        ax_month.plot(dfm["month"].astype(str), dfm["commits"], marker="o", color=_PALETTE)
        ax_month.set_title("Commits per Month")
        step = max(1, len(dfm) // 12)
        ax_month.set_xticks(range(0, len(dfm), step))
        ax_month.set_xticklabels(dfm["month"].astype(str).iloc[::step], rotation=60, ha="right", fontsize=7)
        ax_month.grid(alpha=0.3)

    # Top repositories.
    ax_repo = fig.add_subplot(gs[1, 1])
    dfr = stats.commits_per_repo.head(10)
    if dfr.empty:
        _empty_axes(ax_repo, "Top Repositories")
    else:
        ax_repo.barh(dfr["full_name"].astype(str), dfr["commits"], color=_PALETTE)
        ax_repo.invert_yaxis()
        ax_repo.set_title("Top Repositories by Commits")
        ax_repo.grid(axis="x", alpha=0.3)

    return _save(fig, path, tight=False)


def generate_all_charts(stats: Statistics, charts_dir: Path) -> list[Path]:
    """Generate every chart and the dashboard. Returns the paths written."""
    charts_dir.mkdir(parents=True, exist_ok=True)
    paths = [
        chart_commits_per_year(stats, charts_dir / "commits_per_year.png"),
        chart_commits_per_month(stats, charts_dir / "commits_per_month.png"),
        chart_commits_by_weekday(stats, charts_dir / "commits_by_weekday.png"),
        chart_top_repositories(stats, charts_dir / "top_repositories.png"),
        chart_commits_by_org(stats, charts_dir / "commits_by_organization.png"),
        build_dashboard(stats, charts_dir / "dashboard.png"),
    ]
    return paths
