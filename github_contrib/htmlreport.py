"""Human-readable report generation: structured Markdown + self-contained HTML.

The HTML report embeds the chart PNGs as base64 data URIs so the single file is
fully portable (no external assets) and easy to share with non-technical users.
"""

from __future__ import annotations

import base64
import html
from pathlib import Path

import pandas as pd

from .insights import Insights, RepoWork
from .logging_config import get_logger
from .statistics import Statistics

log = get_logger("htmlreport")

# Charts embedded into the HTML report, in display order.
_REPORT_CHARTS: list[tuple[str, str]] = [
    ("commits_per_year.png", "Commits per Year"),
    ("commits_per_month.png", "Commits per Month"),
    ("commits_by_weekday.png", "Commits by Day of Week"),
    ("top_repositories.png", "Top Repositories"),
    ("commits_by_organization.png", "Commits by Organization"),
]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _fmt_date(value: object) -> str:
    text = str(value or "")
    return text[:10] if "T" in text else text


def _metric_pairs(summary: dict[str, object], insights: Insights) -> list[tuple[str, object]]:
    ins = insights.to_summary_dict()
    return [
        ("Lifetime commits", summary.get("total_lifetime_commits", 0)),
        ("Pull requests", summary.get("total_pull_requests", 0)),
        ("Merged PRs", summary.get("merged_pull_requests", 0)),
        ("Repos contributed", summary.get("repositories_contributed_to", 0)),
        ("Organizations", summary.get("organizations_contributed_to", 0)),
        ("Active days", ins.get("active_days", 0)),
        ("Longest streak (days)", ins.get("longest_daily_streak", 0)),
        ("Avg commits / active week", ins.get("avg_commits_per_active_week", 0)),
    ]


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def _md_table(df: pd.DataFrame, columns: list[str] | None = None, limit: int | None = None) -> str:
    if df.empty:
        return "_No data._\n"
    cols = columns or list(df.columns)
    rows = df[cols].head(limit) if limit else df[cols]
    head = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    body = "\n".join(
        "| " + " | ".join(str(v).replace("|", "\\|") for v in row) + " |"
        for row in rows.itertuples(index=False, name=None)
    )
    return f"{head}\n{sep}\n{body}\n"


def render_markdown(stats: Statistics, insights: Insights, summary: dict[str, object]) -> str:
    ins = insights.to_summary_dict()
    out: list[str] = []
    out.append("# GitHub Contribution Report\n")
    out.append(f"**Users:** {summary.get('tracked_users', '')}  ")
    out.append(f"**Generated:** {_fmt_date(summary.get('generated_at'))}  ")
    out.append(
        f"**Activity:** {_fmt_date(summary.get('first_contribution_date'))} "
        f"→ {_fmt_date(summary.get('latest_contribution_date'))}\n"
    )

    out.append("## At a glance\n")
    for label, value in _metric_pairs(summary, insights):
        out.append(f"- **{label}:** {value}")
    out.append("")

    out.append("## Activity insights\n")
    out.append(f"- **Busiest day of week:** {ins.get('busiest_day') or 'n/a'}")
    out.append(f"- **Busiest month:** {ins.get('busiest_month') or 'n/a'}")
    out.append(f"- **Primary languages:** {ins.get('primary_languages') or 'n/a'}")
    if insights.languages:
        out.append("\n**Languages worked in:**\n")
        lang_df = pd.DataFrame(insights.languages, columns=["language", "repos", "commits"])
        out.append(_md_table(lang_df))
    out.append("")

    out.append("## Contributors\n")
    out.append(_md_table(stats.per_user))
    out.append("")

    if not stats.organizations.empty:
        out.append("## Organizations\n")
        out.append(_md_table(
            stats.organizations,
            ["login", "repos_contributed", "commit_count", "pr_count", "merged_pr_count"],
        ))
        out.append("")

    out.append("## Work breakdown by repository\n")
    out.append(
        "_Descriptions are derived from pull-request titles, branch names and "
        "recurring commit keywords - so the work is explained even when individual "
        "commit messages are terse._\n"
    )
    for work in insights.repo_work:
        visibility = "private" if work.is_private else "public"
        lang = f" · {work.language}" if work.language else ""
        out.append(f"### {work.full_name}  \n")
        out.append(f"_{visibility}{lang} — {work.headline()}_\n")
        if work.highlights:
            out.append("**What was worked on:**\n")
            for item in work.highlights:
                out.append(f"- {item}")
            out.append("")
        if work.themes:
            out.append(f"**Recurring themes:** {', '.join(work.themes)}\n")
        if work.conv_types:
            kinds = ", ".join(f"{k}: {v}" for k, v in work.conv_types.items())
            out.append(f"**Commit types:** {kinds}\n")
    out.append("")

    out.append("## Top repositories by commits\n")
    out.append(_md_table(stats.top_repositories, limit=25))

    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_CSS = """
:root { --accent:#305496; --accent2:#4472c4; --bg:#f6f8fb; --card:#fff; --ink:#1f2d3d; --muted:#6b7a90; --line:#e3e8ef; }
* { box-sizing:border-box; }
body { margin:0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
       color:var(--ink); background:var(--bg); line-height:1.55; }
.wrap { max-width:1080px; margin:0 auto; padding:0 20px 64px; }
header.hero { background:linear-gradient(135deg,var(--accent),var(--accent2)); color:#fff; padding:36px 0 28px; margin-bottom:28px; }
header.hero .wrap { padding-bottom:0; }
header.hero h1 { margin:0 0 6px; font-size:28px; }
header.hero .meta { opacity:.92; font-size:14px; }
h2 { font-size:20px; margin:36px 0 14px; padding-bottom:6px; border-bottom:2px solid var(--line); }
h3 { font-size:16px; margin:22px 0 4px; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:14px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px 18px; box-shadow:0 1px 2px rgba(16,42,80,.04); }
.card .v { font-size:26px; font-weight:700; color:var(--accent); }
.card .l { font-size:12.5px; color:var(--muted); margin-top:2px; text-transform:uppercase; letter-spacing:.03em; }
table { border-collapse:collapse; width:100%; background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; font-size:14px; }
th,td { text-align:left; padding:9px 12px; border-bottom:1px solid var(--line); }
th { background:var(--accent); color:#fff; font-weight:600; }
tr:last-child td { border-bottom:none; }
tr:nth-child(even) td { background:#fafbfe; }
.table-scroll { overflow-x:auto; }
.charts { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:18px; }
.charts figure { margin:0; background:var(--card); border:1px solid var(--line); border-radius:12px; padding:10px; }
.charts img { width:100%; height:auto; display:block; border-radius:6px; }
.charts figcaption { font-size:12.5px; color:var(--muted); text-align:center; padding-top:6px; }
.repo { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px 18px; margin:14px 0; }
.repo .name { font-size:16px; font-weight:700; }
.repo .sub { color:var(--muted); font-size:13px; margin:2px 0 10px; }
.badge { display:inline-block; font-size:11px; padding:1px 8px; border-radius:999px; margin-left:8px; vertical-align:middle; }
.badge.private { background:#fdecea; color:#b3261e; }
.badge.public { background:#e7f4ea; color:#1e7d34; }
.repo ul { margin:6px 0 0; padding-left:20px; }
.repo li { margin:2px 0; }
.themes { margin-top:10px; }
.chip { display:inline-block; background:#eef2f9; color:#33476a; border-radius:999px; padding:2px 10px; margin:3px 4px 0 0; font-size:12.5px; }
.note { color:var(--muted); font-size:13.5px; font-style:italic; }
footer { color:var(--muted); font-size:12.5px; margin-top:40px; text-align:center; }
"""


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def _html_table(df: pd.DataFrame, columns: list[str] | None = None, limit: int | None = None) -> str:
    if df.empty:
        return '<p class="note">No data.</p>'
    cols = columns or list(df.columns)
    rows = df[cols].head(limit) if limit else df[cols]
    head = "".join(f"<th>{_esc(c)}</th>" for c in cols)
    body = "".join(
        "<tr>" + "".join(f"<td>{_esc(v)}</td>" for v in row) + "</tr>"
        for row in rows.itertuples(index=False, name=None)
    )
    return f'<div class="table-scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _embed_chart(charts_dir: Path, filename: str) -> str | None:
    path = charts_dir / filename
    if not path.exists():
        return None
    try:
        data = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None
    return f"data:image/png;base64,{data}"


def render_html(
    stats: Statistics,
    insights: Insights,
    summary: dict[str, object],
    charts_dir: Path,
    include_charts: bool = True,
) -> str:
    ins = insights.to_summary_dict()
    parts: list[str] = []

    parts.append("<header class='hero'><div class='wrap'>")
    parts.append("<h1>GitHub Contribution Report</h1>")
    parts.append(
        f"<div class='meta'>Users: <strong>{_esc(summary.get('tracked_users'))}</strong> · "
        f"Activity {_esc(_fmt_date(summary.get('first_contribution_date')))} → "
        f"{_esc(_fmt_date(summary.get('latest_contribution_date')))} · "
        f"Generated {_esc(_fmt_date(summary.get('generated_at')))}</div>"
    )
    parts.append("</div></header>")

    parts.append("<div class='wrap'>")

    # Metric cards
    parts.append("<section><h2>At a glance</h2><div class='cards'>")
    for label, value in _metric_pairs(summary, insights):
        parts.append(f"<div class='card'><div class='v'>{_esc(value)}</div><div class='l'>{_esc(label)}</div></div>")
    parts.append("</div></section>")

    # Activity insights
    parts.append("<section><h2>Activity insights</h2><div class='cards'>")
    for label, value in [
        ("Busiest day", ins.get("busiest_day") or "n/a"),
        ("Busiest month", ins.get("busiest_month") or "n/a"),
        ("Primary languages", ins.get("primary_languages") or "n/a"),
    ]:
        parts.append(f"<div class='card'><div class='v' style='font-size:18px'>{_esc(value)}</div><div class='l'>{_esc(label)}</div></div>")
    parts.append("</div></section>")

    # Charts
    if include_charts:
        figures: list[str] = []
        for filename, caption in _REPORT_CHARTS:
            uri = _embed_chart(charts_dir, filename)
            if uri:
                figures.append(
                    f"<figure><img alt='{_esc(caption)}' src='{uri}'/>"
                    f"<figcaption>{_esc(caption)}</figcaption></figure>"
                )
        if figures:
            parts.append("<section><h2>Charts</h2><div class='charts'>")
            parts.extend(figures)
            parts.append("</div></section>")

    # Contributors
    parts.append("<section><h2>Contributors</h2>")
    parts.append(_html_table(stats.per_user))
    parts.append("</section>")

    # Organizations
    if not stats.organizations.empty:
        parts.append("<section><h2>Organizations</h2>")
        parts.append(_html_table(
            stats.organizations,
            ["login", "repos_contributed", "commit_count", "pr_count", "merged_pr_count"],
        ))
        parts.append("</section>")

    # Work breakdown
    parts.append("<section><h2>Work breakdown by repository</h2>")
    parts.append(
        "<p class='note'>Descriptions are derived from pull-request titles, branch "
        "names and recurring commit keywords — so the work is explained even when "
        "individual commit messages are terse.</p>"
    )
    for work in insights.repo_work:
        parts.append(_render_repo_card(work))
    parts.append("</section>")

    # Top repos
    parts.append("<section><h2>Top repositories by commits</h2>")
    parts.append(_html_table(stats.top_repositories, limit=25))
    parts.append("</section>")

    parts.append("<footer>Generated by github-contrib · deterministic summary (no AI)</footer>")
    parts.append("</div>")

    body = "\n".join(parts)
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>GitHub Contribution Report</title>"
        f"<style>{_CSS}</style></head><body>{body}</body></html>"
    )


def _render_repo_card(work: RepoWork) -> str:
    badge = "private" if work.is_private else "public"
    lang = f" · {_esc(work.language)}" if work.language else ""
    chips = "".join(f"<span class='chip'>{_esc(t)}</span>" for t in work.themes)
    items = "".join(f"<li>{_esc(h)}</li>" for h in work.highlights)
    items_html = f"<ul>{items}</ul>" if items else "<p class='note'>No PR/branch descriptions available.</p>"
    themes_html = f"<div class='themes'>Recurring themes: {chips}</div>" if chips else ""
    return (
        "<div class='repo'>"
        f"<div class='name'>{_esc(work.full_name)}<span class='badge {badge}'>{badge}</span></div>"
        f"<div class='sub'>{_esc(work.headline())}{lang}</div>"
        f"{items_html}{themes_html}"
        "</div>"
    )


def export_reports(
    output_dir: Path,
    stats: Statistics,
    insights: Insights,
    summary: dict[str, object],
    charts_dir: Path,
    include_charts: bool = True,
) -> list[Path]:
    """Write report.md and report.html. Returns the paths written."""
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / "report.md"
    html_path = output_dir / "report.html"
    md_path.write_text(render_markdown(stats, insights, summary), encoding="utf-8")
    html_path.write_text(
        render_html(stats, insights, summary, charts_dir, include_charts),
        encoding="utf-8",
    )
    log.info("wrote %s and %s", md_path.name, html_path.name)
    return [md_path, html_path]
