"""Human-readable report generation: structured Markdown + self-contained HTML.

The HTML report embeds the chart PNGs as base64 data URIs so the single file is
fully portable (no external assets) and easy to share with non-technical users.

Every value from GitHub (commit messages, repository names, ...) is escaped,
and a Content-Security-Policy in the page only lets the report's own script
run and loads nothing from the network — also while the server renders the
PDF in a headless browser.
"""

from __future__ import annotations

import base64
import html
import math
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from .analytics import (
    SMALL_COMMIT_LINES,
    WEEKDAYS,
    WORK_TYPES,
    Analytics,
    Rhythm,
    TechUse,
    Timeline,
    clean_title,
    human_duration,
)
from .filters import ReportScope
from .insights import ExecSummary, Insights, RepoWork, build_exec_summary
from .logging_config import get_logger
from .reportscript import REPORT_CSP, SCRIPT, SCRIPT_HASH  # noqa: F401 - SCRIPT_HASH re-exported
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


def _metric_pairs(
    summary: dict[str, object], insights: Insights, scope: ReportScope | None = None
) -> list[tuple[str, object]]:
    ins = insights.to_summary_dict()
    commits_label = "Commits in period" if scope is not None and scope.has_period else "Lifetime commits"
    pairs: list[tuple[str, object]] = [
        (commits_label, summary.get("total_lifetime_commits", 0)),
        ("Pull requests", summary.get("total_pull_requests", 0)),
        ("Merged PRs", summary.get("merged_pull_requests", 0)),
        ("Repos contributed", summary.get("repositories_contributed_to", 0)),
        ("Organizations", summary.get("organizations_contributed_to", 0)),
        ("Active days", ins.get("active_days", 0)),
        ("Longest streak (days)", ins.get("longest_daily_streak", 0)),
        ("Avg commits / active week", ins.get("avg_commits_per_active_week", 0)),
    ]
    if summary.get("total_lines_added"):
        added = int(summary["total_lines_added"])  # type: ignore[arg-type]
        deleted = int(summary.get("total_lines_deleted", 0))  # type: ignore[arg-type]
        pairs += [
            ("Lines added", f"+{added:,}"),
            ("Lines deleted", f"−{deleted:,}"),
            ("Net lines", f"{int(summary.get('net_lines', 0)):+,}"),  # type: ignore[arg-type]
            ("Files touched", f"{int(summary.get('total_files_changed', 0)):,}"),  # type: ignore[arg-type]
        ]
    if summary.get("merge_commits"):
        pairs.append(("Merge commits", summary["merge_commits"]))
    return pairs


# ---------------------------------------------------------------------------
# Report scope: period, completeness, method
# ---------------------------------------------------------------------------

def _day(moment: datetime) -> str:
    return f"{moment.day} {moment.strftime('%b %Y')}"


def period_text(scope: ReportScope | None) -> str:
    """'1 Jan 2026 – 31 Mar 2026 (Asia/Kolkata)', or 'All time'."""
    if scope is None or not scope.has_period:
        return "All time" + (f" ({scope.timezone})" if scope and scope.timezone != "UTC" else "")
    if scope.since and scope.until:
        span = f"{_day(scope.since)} – {_day(scope.until)}"
    elif scope.since:
        span = f"From {_day(scope.since)}"
    else:
        span = f"Until {_day(scope.until)}"  # type: ignore[arg-type]
    return f"{span} ({scope.timezone})"


def method_items(scope: ReportScope | None) -> list[str]:
    """How the data was collected and counted, in plain sentences."""
    if scope is None:
        return []
    meta = scope.collection
    items: list[str] = [f"Reporting period: {period_text(scope)}. Commits count by author date, pull requests by the date they were opened."]
    if meta.get("scan_all_branches", True) and meta.get("collect_prs", True):
        items.append(
            "Every branch of every repository the accounts can access was scanned, plus "
            "the default branch of upstream projects and the commits of each pull "
            "request (which recovers work on deleted branches)."
        )
    elif meta.get("scan_all_branches", True):
        items.append(
            "Every branch of every repository the accounts can access was scanned, plus "
            "the default branch of upstream projects. Pull requests were not read, so "
            "commits that exist only in a pull request (for example on a deleted branch) "
            "are not included."
        )
    else:
        items.append("Only each repository's default branch was scanned.")
    emails = meta.get("author_emails") or []
    if emails:
        items.append(f"Commits were also matched by {len(emails)} commit email address(es) not linked to the GitHub account.")
    items.append("Forked repositories were skipped." if meta.get("skip_forks") else "Forked repositories were included.")
    if not meta.get("collect_prs", True):
        items.append("Pull requests were not collected.")
    if meta.get("fetch_commit_stats", True):
        line = "Line counts exclude merge commits, whose diff repeats work from the merged branch."
        if scope.commits_without_line_stats:
            line += f" {scope.commits_without_line_stats} commit(s) have no line statistics (see Data completeness)."
        items.append(line)
    else:
        items.append("Line statistics were not collected.")
    if scope.excluded_owners:
        items.append("Repositories owned by " + ", ".join(scope.excluded_owners) + " are excluded.")
    dup = scope.duplicates
    if dup.total:
        parts = []
        if dup.same_commit:
            parts.append(f"{dup.same_commit} copy/copies of the same commit in another repository (fork or mirror)")
        if dup.rebased_copies:
            parts.append(f"{dup.rebased_copies} rebased or cherry-picked copy/copies")
        if dup.squashed:
            parts.append(f"{dup.squashed} original commit(s) of squash-merged pull requests")
        items.append("Each change is counted once: " + "; ".join(parts) + " were not counted again.")
    if meta.get("collected_at"):
        items.append(f"Data collected {str(meta['collected_at'])[:16].replace('T', ' ')} UTC.")
    return items


def _render_completeness(scope: ReportScope | None) -> str:
    if scope is None:
        return ""
    if not scope.notes:
        return (
            "<section id='completeness'><h2>Data completeness</h2>"
            "<p class='note'>No gaps detected: every repository, branch and request was read successfully.</p></section>"
        )
    items = "".join(f"<li>{_esc(note)}</li>" for note in scope.notes)
    return (
        "<section id='completeness'><h2>Data completeness</h2>"
        "<div class='gaps'><p><strong>Some data could not be read, so this report may be "
        "missing contributions:</strong></p>"
        f"<ul>{items}</ul></div></section>"
    )


def _render_method(scope: ReportScope | None) -> str:
    items = method_items(scope)
    if not items:
        return ""
    lis = "".join(f"<li>{_esc(item)}</li>" for item in items)
    return f"<section id='method'><h2>How this report was compiled</h2><ul class='method'>{lis}</ul></section>"


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


def render_markdown(
    stats: Statistics,
    insights: Insights,
    summary: dict[str, object],
    scope: ReportScope | None = None,
) -> str:
    ins = insights.to_summary_dict()
    out: list[str] = []
    out.append("# GitHub Contribution Report\n")
    out.append(f"**Users:** {summary.get('tracked_users', '')}  ")
    out.append(f"**Period:** {period_text(scope)}  ")
    out.append(f"**Generated:** {_fmt_date(summary.get('generated_at'))}  ")
    out.append(
        f"**Activity:** {_fmt_date(summary.get('first_contribution_date'))} "
        f"→ {_fmt_date(summary.get('latest_contribution_date'))}\n"
    )

    out.append("## At a glance\n")
    for label, value in _metric_pairs(summary, insights, scope):
        out.append(f"- **{label}:** {value}")
    out.append("")

    if scope is not None and scope.notes:
        out.append("## Data completeness\n")
        out.append("Some data could not be read, so this report may be missing contributions:\n")
        out.extend(f"- {note}" for note in scope.notes)
        out.append("")

    exec_summary = build_exec_summary(insights, summary)
    out.append("## Executive summary — contribution highlights\n")
    for paragraph in exec_summary.overview:
        out.append(paragraph + "\n")
    if exec_summary.key_projects:
        out.append("**Key projects & impact:**\n")
        for w in exec_summary.key_projects:
            top_work = "; ".join(w.highlights[:3])
            suffix = f" — key work: {top_work}" if top_work else ""
            out.append(f"- **{w.full_name}** — {w.headline()}{suffix}")
        out.append("")
    if exec_summary.features:
        out.append("**Features & improvements delivered:**\n")
        for repo, feature in exec_summary.features:
            out.append(f"- {feature} _({repo})_")
        out.append("")
    if exec_summary.strengths:
        out.append("**Profile strengths:**\n")
        for label, value in exec_summary.strengths:
            out.append(f"- **{label}:** {value}")
        out.append("")

    out.extend(_markdown_stack(stats.analytics))
    out.extend(_markdown_analytics(stats.analytics, scope))

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
        if work.technologies:
            out.append(f"**Stack:** {', '.join(work.technologies)}\n")
        if work.themes:
            out.append(f"**Recurring themes:** {', '.join(work.themes)}\n")
        if work.conv_types:
            kinds = ", ".join(f"{k}: {v}" for k, v in work.conv_types.items())
            out.append(f"**Commit types:** {kinds}\n")
    out.append("")

    out.append("## Top repositories by commits\n")
    out.append(_md_table(stats.top_repositories, limit=25))

    method = method_items(scope)
    if method:
        out.append("\n## How this report was compiled\n")
        out.extend(f"- {item}" for item in method)

    # GitHub text could carry HTML that a Markdown viewer would render.
    text = "\n".join(out) + "\n"
    return text.replace("<", "&lt;").replace(">", "&gt;")


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
.stackline { margin:-4px 0 10px; font-size:13px; color:var(--muted); }
.stackline .chip { background:#e8f3ec; color:#1d5b33; }
.chip { display:inline-block; background:#eef2f9; color:#33476a; border-radius:999px; padding:2px 10px; margin:3px 4px 0 0; font-size:12.5px; }
.note { color:var(--muted); font-size:13.5px; font-style:italic; }
.impact { display:flex; flex-wrap:wrap; gap:8px; margin:8px 0 10px; }
.istat { font-size:12.5px; font-weight:600; padding:2px 10px; border-radius:999px; background:#f0f2f7; }
.istat.add { color:#1e7d34; background:#e7f4ea; }
.istat.del { color:#b3261e; background:#fdecea; }
.istat.net { background:#f0f2f7; }
.istat.files { color:var(--muted); }
.bigcommit { font-size:12.5px; color:var(--muted); margin-bottom:8px; }
footer { color:var(--muted); font-size:12.5px; margin-top:40px; text-align:center; }
.gaps { background:#fff8e1; border:1px solid #f3d27a; border-radius:12px; padding:12px 18px; font-size:14px; }
.gaps ul { margin:6px 0 0; padding-left:20px; }
.gaps li { margin:3px 0; }
ul.method { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 18px 14px 36px; font-size:14px; }
ul.method li { margin:4px 0; }
/* Executive summary */
.exec { background:var(--card); border:1px solid var(--line); border-left:4px solid var(--accent); border-radius:12px; padding:20px 22px; }
.exec p.lead { font-size:15px; margin:8px 0; }
.exec h3 { margin:20px 0 8px; color:var(--accent); }
.exec .proj { margin:10px 0 14px; padding-left:14px; border-left:3px solid var(--accent2); }
.exec .proj .pname { font-weight:700; }
.exec .proj .pmeta { color:var(--muted); font-size:13px; margin:1px 0 3px; }
.exec .proj .pwork { font-size:13.5px; }
.exec ul.features { margin:6px 0 0; padding-left:20px; }
.exec ul.features li { margin:4px 0; }
.exec .frepo { color:var(--muted); font-size:12px; }
/* Per-repo commit lists */
details.commitlist { margin-top:12px; border-top:1px dashed var(--line); padding-top:8px; }
details.commitlist summary { cursor:pointer; font-weight:600; color:var(--accent); font-size:13.5px; }
details.commitlist table { font-size:12.5px; margin-top:8px; }
details.commitlist td.cdate { white-space:nowrap; color:var(--muted); }
details.commitlist td.sha a { font-family:ui-monospace,Consolas,monospace; font-size:12px; }
details.commitlist td.clines { white-space:nowrap; }
details.commitlist .ladd { color:#1e7d34; }
details.commitlist .ldel { color:#b3261e; }
button.toggle { background:var(--card); color:var(--accent); border:1px solid var(--line); border-radius:8px; padding:6px 14px; font-size:13px; cursor:pointer; }
button.toggle:hover { background:#eef2f9; }
/* Repository dashboard */
.dash td.dname { font-weight:600; white-space:nowrap; }
.dash td.dnum { text-align:right; font-weight:700; color:var(--accent); white-space:nowrap; }
.dash td.dshare { text-align:right; color:var(--muted); white-space:nowrap; }
.dash td.dbarcell { width:34%; min-width:140px; }
.dash .dbar { height:14px; border-radius:4px; background:linear-gradient(90deg,var(--accent),var(--accent2));
              min-width:3px; print-color-adjust:exact; -webkit-print-color-adjust:exact; }
.dash td.dmeta { color:var(--muted); font-size:12.5px; white-space:nowrap; }
.dash .nobr { white-space:nowrap; }
/* Insights & analytics */
#analytics h3 { margin:26px 0 10px; }
#analytics .hl h3 { margin:0 0 4px; }
#analytics .card .v { font-size:22px; white-space:nowrap; }
.hl ul { margin:6px 0 0; padding-left:20px; }
.hl li { margin:5px 0; }
.agrid { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:14px; margin-top:14px; }
.atable { margin-top:14px; }
.atable h4, .shipped h4 { margin:0 0 6px; font-size:13.5px; color:var(--muted); font-weight:600; }
table.bars { font-size:13px; }
table.bars td { padding:5px 10px; }
table.bars td.alabel, table.bars td.anum, table.bars td.ashare { white-space:nowrap; width:1%; }
table.bars td.anum, table.bars td.ashare, .abig td.anum { text-align:right; white-space:nowrap; }
table.bars td.ashare { color:var(--muted); }
.abar { height:12px; border-radius:4px; background:linear-gradient(90deg,var(--accent),var(--accent2));
        print-color-adjust:exact; -webkit-print-color-adjust:exact; }
.abig table { font-size:13px; }
.atag { display:inline-block; font-size:11px; font-weight:500; color:var(--muted); border:1px solid var(--line);
        border-radius:6px; padding:0 5px; margin-left:4px; }
.anote { font-size:12.5px; color:var(--muted); margin:8px 0 0; }
.shipped { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:12px; }
.shipped .sq { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px 16px; }
.shipped ul { margin:0; padding-left:18px; font-size:13.5px; }
.shipped li { margin:3px 0; }
.shipped .frepo { color:var(--muted); font-size:12px; }
table.heat { table-layout:fixed; border-collapse:separate; border-spacing:2px; border:none; background:none; }
table.heat th { background:none; color:var(--muted); font-weight:500; font-size:10.5px; padding:0 1px; text-align:left; }
table.heat th.day { width:36px; }
table.heat td { height:18px; padding:0; border:none; border-radius:3px; background:#eef2f8;
                print-color-adjust:exact; -webkit-print-color-adjust:exact; }
.tl { margin-top:6px; }
.tlrow { display:grid; grid-template-columns:minmax(140px,28%) 1fr; gap:12px; align-items:center;
         padding:5px 0; border-bottom:1px solid var(--line); }
.tlrow.tlaxis { border-bottom:none; padding:0 0 2px; }
.tlname { font-size:13px; line-height:1.3; overflow-wrap:anywhere; }
.tlname b { font-weight:600; }
.tlname span { display:block; color:var(--muted); font-size:11.5px; }
.tltrack { position:relative; height:20px; }
.tlaxis .tltrack { height:15px; }
.tltick { position:absolute; top:0; bottom:0; border-left:1px dashed #d3dbe8; }
.tllabel { position:absolute; top:0; padding-left:3px; font-size:10.5px; color:var(--muted); white-space:nowrap; }
.tlspan, .tlm { position:absolute; print-color-adjust:exact; -webkit-print-color-adjust:exact; }
.tlspan { top:9px; height:2px; background:#c3cee0; }
.tlm { top:3px; height:14px; min-width:2px; border-radius:2px; }
/* Commit timeline appendix */
#commit-timeline tr.month th { background:#eef2f9; color:#33476a; font-size:12.5px; letter-spacing:.04em;
                               text-transform:uppercase; print-color-adjust:exact; -webkit-print-color-adjust:exact; }
#commit-timeline td.cnum { color:var(--muted); text-align:right; white-space:nowrap; }
/* Print / PDF */
@media print {
  /* Backgrounds print even with the print dialog's "Background graphics"
     off: the header and table headings are white text on them. */
  body { background:#fff; print-color-adjust:exact; -webkit-print-color-adjust:exact; }
  .wrap { max-width:none; padding:0 4px; }
  /* Tables fit the page width (on screen they scroll sideways): smaller
     text, and long values wrap. */
  .table-scroll { overflow:visible; }
  table { font-size:11.5px; }
  th, td { padding:6px 8px; overflow-wrap:break-word; }
  .dash { font-size:11.5px; }
  .dash th, .dash td { padding:6px 8px; }
  .dash td.dbarcell { width:16%; min-width:0; }
  .dash td.dname, .dash td.dmeta { white-space:normal; overflow-wrap:break-word; }
  .dash td.dmeta { font-size:11px; }
  .card, .charts figure, .exec .proj, tr { break-inside:avoid; }
  .atable, .shipped .sq, #analytics .cards, .tlrow, .tlwrap { break-inside:avoid; }
  .tllabel { font-size:9.5px; }
  #analytics h3, #stack h2, #stack > .note { break-after:avoid; }
  section { break-inside:auto; }
  h2 { break-after:avoid; }
  a { color:inherit; text-decoration:none; }
  .noprint { display:none !important; }
  /* Raw commit data prints ONLY as the chronological appendix at the end;
     collapsed per-repo lists (mid-document) disappear entirely. */
  details.commitlist:not([open]) { display:none; }
  #commit-timeline { break-before:page; }
  /* Compact appendix: about 40% fewer pages, and each page costs the
     browser CPU time (minutes on a small cloud server). */
  #commit-timeline table { font-size:9.5px; }
  #commit-timeline td, #commit-timeline th { padding:2px 6px; }
  /* Fixed columns: the same on every page, also when the PDF is printed in
     parts (each part would otherwise size them to its own rows). */
  #commit-timeline table { table-layout:fixed; }
  #commit-timeline thead th:nth-child(1) { width:5%; }
  #commit-timeline thead th:nth-child(2) { width:10%; }
  #commit-timeline thead th:nth-child(3) { width:19%; }
  #commit-timeline thead th:nth-child(4) { width:10%; }
  #commit-timeline thead th:nth-child(6) { width:8%; }
  #commit-timeline thead th:nth-child(7) { width:10%; }
  #commit-timeline td { overflow-wrap:anywhere; }
  #commit-timeline td.clines { white-space:normal; }
  #commit-timeline td.sha a { font-size:10.5px; }
  /* A long appendix is printed in parts (#print=FROM-TO, see pdfexport):
     each part shows only its rows, and later parts only the appendix. */
  #commit-timeline tr.pskip { display:none; }
  body.pcont header.hero, body.pcont .wrap > :not(#commit-timeline):not(footer),
  body.pcont #commit-timeline > :not(details), body.pcont #commit-timeline summary,
  body.pmore footer { display:none; }
  body.pcont #commit-timeline { break-before:auto; }
  body.pcont #commit-timeline details { margin-top:0; border-top:none; padding-top:0; }
  details.commitlist summary { list-style:none; }
  details.commitlist summary::-webkit-details-marker { display:none; }
}
"""

_SCRIPT = SCRIPT


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def _safe_url(value: object) -> str:
    """Only https links (GitHub's own) may become clickable."""
    url = str(value or "")
    return url if url.startswith("https://") else ""


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


def _commits_by_repo(commits_df: pd.DataFrame) -> dict[str, list[dict]]:
    """Group the commits table by repository, newest first."""
    if commits_df is None or commits_df.empty:
        return {}
    grouped: dict[str, list[dict]] = {}
    for row in commits_df.fillna("").to_dict("records"):
        grouped.setdefault(str(row.get("full_name", "")), []).append(row)
    for rows in grouped.values():
        rows.sort(key=lambda r: str(r.get("authored_date") or ""), reverse=True)
    return grouped


def _render_commit_list(rows: list[dict]) -> str:
    """A collapsible table with every commit of one repository."""
    if not rows:
        return ""
    body: list[str] = []
    for r in rows:
        sha = str(r.get("sha") or "")[:7]
        url = _safe_url(r.get("url"))
        sha_html = (
            f"<a href='{_esc(url)}' target='_blank' rel='noopener'>{_esc(sha)}</a>"
            if url else _esc(sha)
        )
        adds = int(r.get("additions") or 0)
        dels = int(r.get("deletions") or 0)
        lines = (
            f"<span class='ladd'>+{adds:,}</span> / <span class='ldel'>−{dels:,}</span>"
            if (adds or dels) else ""
        )
        body.append(
            "<tr>"
            f"<td class='cdate'>{_esc(_fmt_date(r.get('authored_date')))}</td>"
            f"<td class='sha'>{sha_html}</td>"
            f"<td>{_esc(str(r.get('branch') or ''))}</td>"
            f"<td class='cmsg'>{_esc(str(r.get('message_first_line') or '')[:140])}</td>"
            f"<td class='clines'>{lines}</td>"
            "</tr>"
        )
    n = len(rows)
    return (
        f"<details class='commitlist'><summary>Show all {n} commit(s)</summary>"
        "<div class='table-scroll'><table>"
        "<thead><tr><th>Date</th><th>Commit</th><th>Branch</th><th>Message</th><th>Lines</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table></div></details>"
    )


def _render_repo_dashboard(insights: Insights) -> str:
    """'Numbers first' dashboard: every repository's commit count and share."""
    works = sorted(insights.repo_work, key=lambda w: (-w.commits, w.full_name))
    if not works:
        return ""
    total = sum(w.commits for w in works) or 1
    max_commits = max(w.commits for w in works) or 1

    rows: list[str] = []
    for w in works:
        width = max(2.0, w.commits / max_commits * 100)
        share = w.commits / total * 100
        pr = f"{w.pull_requests} ({w.merged_pull_requests} merged)" if w.pull_requests else "—"
        span = ""
        if w.first_activity and w.last_activity:
            # Each date stays on one line when the period wraps (in print).
            span = (
                f"<span class='nobr'>{w.first_activity.date()}</span> → "
                f"<span class='nobr'>{w.last_activity.date()}</span>"
            )
        rows.append(
            "<tr>"
            f"<td class='dname'>{_esc(w.full_name)}</td>"
            f"<td class='dnum'>{w.commits:,}</td>"
            f"<td class='dbarcell'><div class='dbar' style='width:{width:.1f}%'></div></td>"
            f"<td class='dshare'>{share:.1f}%</td>"
            f"<td class='dmeta'>{_esc(pr)}</td>"
            f"<td class='dmeta'>{_esc(w.language or '—')}</td>"
            f"<td class='dmeta'>{span}</td>"
            "</tr>"
        )
    return (
        "<section id='repo-dashboard'><h2>Repository dashboard</h2>"
        f"<p class='note'>{total:,} commits across {len(works)} repositories — every "
        "repository's share of the delivered work, largest first.</p>"
        "<div class='table-scroll'><table class='dash'>"
        "<thead><tr><th>Repository</th><th>Commits</th><th></th><th>Share</th>"
        "<th>PRs</th><th>Language</th><th>Active period</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div></section>"
    )


_MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"]


def _month_label(iso_date: str) -> str:
    """'2026-02-10T…' -> 'February 2026' ('' when the date is missing)."""
    if len(iso_date) < 7 or not iso_date[:4].isdigit():
        return ""
    try:
        return f"{_MONTH_NAMES[int(iso_date[5:7]) - 1]} {iso_date[:4]}"
    except (ValueError, IndexError):
        return ""


def _render_commit_timeline(commits_df: pd.DataFrame) -> str:
    """Appendix: every commit across all repos in ascending time order,
    broken up by month headers so the story reads chronologically."""
    if commits_df is None or commits_df.empty:
        return ""
    rows = commits_df.fillna("").to_dict("records")
    rows.sort(key=lambda r: str(r.get("authored_date") or "9999"))  # oldest first

    body: list[str] = []
    current_month = None
    for i, r in enumerate(rows, start=1):
        month = _month_label(str(r.get("authored_date") or ""))
        if month and month != current_month:
            current_month = month
            body.append(f"<tr class='month'><th colspan='7'>{_esc(month)}</th></tr>")
        sha = str(r.get("sha") or "")[:7]
        url = _safe_url(r.get("url"))
        sha_html = (
            f"<a href='{_esc(url)}' target='_blank' rel='noopener'>{_esc(sha)}</a>"
            if url else _esc(sha)
        )
        adds = int(r.get("additions") or 0)
        dels = int(r.get("deletions") or 0)
        lines = (
            f"<span class='ladd'>+{adds:,}</span> / <span class='ldel'>−{dels:,}</span>"
            if (adds or dels) else ""
        )
        body.append(
            "<tr>"
            f"<td class='cnum'>{i}</td>"
            f"<td class='cdate'>{_esc(_fmt_date(r.get('authored_date')))}</td>"
            f"<td class='dname'>{_esc(str(r.get('full_name') or ''))}</td>"
            f"<td>{_esc(str(r.get('branch') or ''))}</td>"
            f"<td class='cmsg'>{_esc(str(r.get('message_first_line') or '')[:140])}</td>"
            f"<td class='sha'>{sha_html}</td>"
            f"<td class='clines'>{lines}</td>"
            "</tr>"
        )
    n = len(rows)
    # data-print-parts: the script can print this appendix in parts (pdfexport).
    return (
        "<section id='commit-timeline' data-print-parts><h2>Appendix — complete commit timeline</h2>"
        f"<p class='note'>All {n} commits across every repository in chronological "
        "order (oldest first), so the work can be followed as it happened.</p>"
        f"<details class='commitlist timeline'><summary>Show the full timeline ({n} commits)</summary>"
        "<div class='table-scroll'><table>"
        "<thead><tr><th>#</th><th>Date</th><th>Repository</th><th>Branch</th>"
        "<th>Message</th><th>Commit</th><th>Lines</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table></div></details></section>"
    )


def _analytics_cards(pairs: list[tuple[str, str]]) -> str:
    cells = "".join(
        f"<div class='card'><div class='v'>{_esc(value)}</div><div class='l'>{_esc(label)}</div></div>"
        for value, label in pairs
    )
    return f"<div class='cards'>{cells}</div>"


def _bar_table(title: str, rows: list[tuple[str, int]]) -> str:
    """A small table of labelled counts, each with a bar and its share."""
    total = sum(n for _label, n in rows) or 1
    peak = max((n for _label, n in rows), default=0) or 1
    body = "".join(
        f"<tr><td class='alabel'>{_esc(label)}</td>"
        f"<td class='abarcell'><div class='abar' style='width:{(max(n / peak * 100, 1.5) if n else 0):.1f}%'></div></td>"
        f"<td class='anum'>{n:,}</td><td class='ashare'>{n / total:.0%}</td></tr>"
        for label, n in rows
    )
    return f"<div class='atable'><h4>{_esc(title)}</h4><table class='bars'><tbody>{body}</tbody></table></div>"


def _heatmap(rhythm: Rhythm) -> str:
    """Commits by weekday (rows) and hour (columns), darker = more."""
    peak = max(max(day) for day in rhythm.heatmap) or 1
    head = "<tr><th class='day'></th>" + "".join(
        f"<th>{hour:02d}</th>" if hour % 3 == 0 else "<th></th>" for hour in range(24)
    ) + "</tr>"
    rows = []
    for day, counts in enumerate(rhythm.heatmap):
        cells = "".join(
            f"<td title='{WEEKDAYS[day]} {hour:02d}:00 · {n} commit{'' if n == 1 else 's'}'"
            + (f" style='background:rgba(48,84,150,{0.15 + 0.85 * n / peak:.2f})'" if n else "")
            + "></td>"
            for hour, n in enumerate(counts)
        )
        rows.append(f"<tr><th class='day'>{WEEKDAYS[day]}</th>{cells}</tr>")
    return f"<table class='heat'><thead>{head}</thead><tbody>{''.join(rows)}</tbody></table>"


def _month_after(day: date) -> date:
    return date(day.year + 1, 1, 1) if day.month == 12 else date(day.year, day.month + 1, 1)


def _months(first: date, last: date) -> str:
    return f"{first:%b %Y}" if (first.year, first.month) == (last.year, last.month) else f"{first:%b %Y} – {last:%b %Y}"


def _timeline(t: Timeline) -> str:
    """When each repository was worked on: a line from its first to its
    latest commit, and a block for every month with commits (darker = more)."""
    origin = date(t.start.year, t.start.month, 1)
    stop = _month_after(t.end)
    span = (stop - origin).days

    def x(day: date) -> float:
        return min(max((day - origin).days / span * 100, 0.0), 100.0)

    months = (stop.year - origin.year) * 12 + stop.month - origin.month
    step = 1 if months <= 8 else 3 if months <= 24 else 6 if months <= 48 else 12
    ticks: list[tuple[float, str]] = []
    day = origin
    while day < stop:
        if (day.month - 1) % step == 0:
            if step == 12:
                label = f"{day.year}"
            elif step == 1 and ticks and day.month != 1:
                label = f"{day:%b}"
            else:
                label = f"{day:%b %Y}"
            ticks.append((x(day), label))
        day = _month_after(day)
    grid = "".join(f"<div class='tltick' style='left:{left:.2f}%'></div>" for left, _label in ticks if left > 0)
    labels = "".join(
        f"<div class='tllabel' style='left:{left:.2f}%'>{_esc(label)}</div>" for left, label in ticks if left < 92
    )
    peak = max((n for row in t.rows for n in row.by_month.values()), default=1)
    rows = [f"<div class='tlrow tlaxis'><div class='tlname'></div><div class='tltrack'>{labels}</div></div>"]
    for row in t.rows:
        blocks = []
        for key, n in sorted(row.by_month.items()):
            month = date(int(key[:4]), int(key[5:7]), 1)
            left = x(month)
            blocks.append(
                f"<div class='tlm' title='{month:%b %Y} · {n:,} commit{'' if n == 1 else 's'}' "
                f"style='left:{left:.2f}%;width:{x(_month_after(month)) - left:.2f}%;"
                f"background:rgba(48,84,150,{0.22 + 0.78 * math.sqrt(n / peak):.2f})'></div>"
            )
        line_left = x(row.first)
        line = (
            f"<div class='tlspan' style='left:{line_left:.2f}%;"
            f"width:{max(x(row.last) - line_left, 0):.2f}%'></div>"
        )
        rows.append(
            f"<div class='tlrow'><div class='tlname'><b>{_esc(row.repository)}</b>"
            f"<span>{row.commits:,} commit{'' if row.commits == 1 else 's'} · {_months(row.first, row.last)}</span></div>"
            f"<div class='tltrack'>{grid}{line}{''.join(blocks)}</div></div>"
        )
    return f"<div class='tl'>{''.join(rows)}</div>"


def _use_table(title: str, uses: list[TechUse], top: int = 10) -> str:
    """Technologies (or languages) with a bar for the commits in the
    repositories that use each, and how many repositories those are."""
    peak = max((u.commits for u in uses), default=0) or 1
    rows = "".join(
        f"<tr title='{_esc(_use_tip(u))}'><td class='alabel'>{_esc(u.name)}</td>"
        f"<td class='abarcell'><div class='abar' style='width:{(max(u.commits / peak * 100, 1.5) if u.commits else 0):.1f}%'></div></td>"
        f"<td class='anum'>{u.commits:,}</td>"
        f"<td class='ashare'>{len(u.repositories)} repo{'' if len(u.repositories) == 1 else 's'}</td></tr>"
        for u in uses[:top]
    )
    more = uses[top:]
    extra = f"<p class='anote'>Also: {_esc(', '.join(u.name for u in more))}.</p>" if more else ""
    return f"<div class='atable'><h4>{_esc(title)}</h4><table class='bars'><tbody>{rows}</tbody></table>{extra}</div>"


def _use_tip(use: TechUse) -> str:
    last = f"last commit {use.last_used:%b %Y}; " if use.last_used else ""
    return f"{last}{', '.join(use.repositories[:8])}{' …' if len(use.repositories) > 8 else ''}"


def _render_stack(a: Analytics | None) -> str:
    """The 'Technologies' section (see stack.py and analytics._stack)."""
    s = a.stack if a is not None else None
    if s is None or not (s.languages or s.technologies):
        return ""

    def repos(n: int) -> str:
        return f"{n} repositor{'y' if n == 1 else 'ies'}"

    unread = f" The technologies of {repos(s.unread)} more couldn't be read." if s.unread else ""
    parts = [
        "<section id='stack'><h2>Technologies</h2>",
        f"<p class='note'>Found in the {repos(s.read)} with commits in this report: GitHub's language "
        "statistics, dependency files (package.json, pubspec.yaml, requirements.txt, go.mod, pom.xml, …) and "
        "telltale files (Dockerfile, CI workflows, Terraform, …). Each is weighted by the commits in the "
        "repositories that use it; a technology being used in a repository doesn't show which parts of it were "
        f"worked on.{unread}</p>",
    ]
    if s.languages:
        parts.append(_use_table("Languages · commits in repositories using each", s.languages, top=8))
    groups = s.by_category()
    if groups:
        parts.append(
            "<div class='agrid'>"
            + "".join(_use_table(category, uses, top=8) for category, uses in groups)
            + "</div>"
        )
    parts.append("</section>")
    return "".join(parts)


def _markdown_stack(a: Analytics | None) -> list[str]:
    s = a.stack if a is not None else None
    if s is None or not (s.languages or s.technologies):
        return []
    out = [
        "## Technologies\n",
        "_Found in the repositories with commits (GitHub's language statistics, dependency files and "
        "telltale files such as Dockerfiles and CI workflows), each weighted by the commits in the "
        "repositories that use it._\n",
    ]
    if s.languages:
        out.append(_md_table(pd.DataFrame(
            [(u.name, u.commits, len(u.repositories)) for u in s.languages],
            columns=["language", "commits in repos using it", "repos"],
        )))
        out.append("")
    for category, uses in s.by_category():
        out.append(f"- **{category}:** " + ", ".join(f"{u.name} ({u.commits:,})" for u in uses))
    out.append("")
    return out


def _render_analytics(a: Analytics | None, scope: ReportScope | None = None) -> str:
    """The 'Insights & analytics' section (see analytics.py for every definition)."""
    if a is None or not (a.work_mix.total or a.work_mix.merges or a.delivery or a.reviews):
        return ""
    parts = ["<section id='analytics'><h2>Insights &amp; analytics</h2>"]
    if a.highlights:
        items = "".join(f"<li>{_esc(line)}</li>" for line in a.highlights)
        parts.append(f"<div class='card hl'><h3>Highlights</h3><ul>{items}</ul></div>")

    d = a.delivery
    if d is not None:
        cards = [(f"{d.merged:,}", f"pull requests merged, of {d.opened:,} opened")]
        if d.merge_rate is not None:
            cards.append((f"{d.merge_rate:.0%}", "of closed pull requests merged"))
        if d.median_hours is not None:
            cards.append((human_duration(d.median_hours), "median time to merge"))
            cards.append((f"{d.within_day / len(d.hours_to_merge):.0%}", "merged within a day"))
        if d.p90_hours is not None and len(d.hours_to_merge) >= 10:
            cards.append((human_duration(d.p90_hours), "9 in 10 merged within"))
        if d.median_size is not None:
            cards.append((f"{d.median_size:,.0f}", "median lines per merged pull request"))
        parts.append("<h3>Delivery</h3>" + _analytics_cards(cards))
        tables = []
        if d.time_buckets:
            tables.append(_bar_table("Time from opening to merge", d.time_buckets))
        if d.size_buckets:
            tables.append(_bar_table(f"Size of merged pull requests ({d.sized:,} with a recorded size)", d.size_buckets))
        if tables:
            parts.append(f"<div class='agrid'>{''.join(tables)}</div>")
        if d.biggest:
            rows = "".join(
                f"<tr><td>{_esc(clean_title(p.title))}</td><td class='dname'>{_esc(p.full_name)}</td>"
                f"<td class='anum'>+{p.additions or 0:,} / −{p.deletions or 0:,}</td>"
                f"<td class='anum'>{p.changed_files or 0:,}</td>"
                f"<td class='anum'>{_esc(human_duration((p.merged_at - p.created_at).total_seconds() / 3600) if p.merged_at and p.created_at else '—')}</td></tr>"
                for p in d.biggest
            )
            parts.append(
                "<div class='atable abig'><h4>Largest merged pull requests</h4><div class='table-scroll'><table>"
                "<thead><tr><th>Pull request</th><th>Repository</th><th>Lines</th><th>Files</th>"
                f"<th>Time to merge</th></tr></thead><tbody>{rows}</tbody></table></div></div>"
            )
        parts.append(
            "<p class='anote'>Time to merge runs from opening a pull request to merging it, so it includes "
            "the wait for review. A pull request's size counts every line its diff changes, including "
            "commits teammates added to it.</p>"
        )

    rv = a.reviews
    if rv is not None:
        cards = [
            (f"{rv.total:,}", "code reviews given"),
            (f"{rv.pull_requests:,}", "pull requests reviewed"),
            (f"{rv.authors:,}", f"teammate{'' if rv.authors == 1 else 's'} whose work was reviewed"),
            (f"{len(rv.repositories):,}", f"repositor{'y' if len(rv.repositories) == 1 else 'ies'}"),
        ]
        parts.append(
            "<h3>Code reviews</h3>" + _analytics_cards(cards)
            + "<div class='agrid'>" + _bar_table("Review outcomes", rv.states)
            + _bar_table("Repositories reviewed most", [(name, n) for name, n in rv.repositories[:8]])
            + "</div><p class='anote'>Reviews submitted on other people's pull requests, from GitHub's "
            "contribution history (reviews of the tracked accounts' own pull requests are left out).</p>"
        )

    mix = a.work_mix
    if mix.total:
        rows = [(kind, mix.counts[kind]) for kind in WORK_TYPES if mix.counts[kind]]
        merges = f" {mix.merges:,} merge commit(s) are left out." if mix.merges else ""
        parts.append(
            "<h3>Work mix</h3>" + _bar_table(f"{mix.total:,} commits by kind of work", rows)
            + "<p class='anote'>Read from the first line of each commit message: conventional prefixes "
            "(feat:, fix:, refactor:, …) first, then words such as “fix”, “add”, “update”, “refactor” or "
            f"“test”. It is an estimate: terse messages land in Other.{merges}</p>"
        )

    if a.quarters:
        lines = any(q.additions or q.deletions for q in a.quarters)
        reviewed = any(q.reviews for q in a.quarters)
        peak = max(q.commits for q in a.quarters) or 1
        head = (
            "<tr><th>Quarter</th><th>Commits</th><th></th><th>vs previous</th><th>PRs merged</th>"
            + ("<th>Reviews</th>" if reviewed else "")
            + "<th>Repos</th><th>Active days</th>" + ("<th>Lines</th>" if lines else "") + "</tr>"
        )
        rows = []
        for q in a.quarters:
            width = max(q.commits / peak * 100, 1.5) if q.commits else 0
            partial = " <span class='atag'>partial</span>" if q.partial else ""
            change = f"{q.change:+.0%}" if q.change is not None else "—"
            line_cell = f"<td class='dmeta'>+{q.additions:,} / −{q.deletions:,}</td>" if lines else ""
            review_cell = f"<td class='dnum'>{q.reviews:,}</td>" if reviewed else ""
            rows.append(
                f"<tr><td class='dname'>{q.label}{partial}</td><td class='dnum'>{q.commits:,}</td>"
                f"<td class='dbarcell'><div class='dbar' style='width:{width:.1f}%'></div></td>"
                f"<td class='dshare'>{change}</td><td class='dnum'>{q.prs_merged:,}</td>{review_cell}"
                f"<td class='dnum'>{q.repositories:,}</td><td class='dnum'>{q.active_days:,}</td>{line_cell}</tr>"
            )
        parts.append(
            "<h3>Quarter by quarter</h3><div class='table-scroll'><table class='dash aq'>"
            f"<thead>{head}</thead><tbody>{''.join(rows)}</tbody></table></div>"
            "<p class='anote'>A partial quarter is one the report (or the work) covers only in part, so it "
            "isn't compared with the quarter before.</p>"
        )
        shipped = [q for q in a.quarters if q.shipped]
        if shipped:
            blocks = "".join(
                f"<div class='sq'><h4>{q.label}</h4><ul>"
                + "".join(
                    f"<li>{_esc(what)} <span class='frepo'>— {_esc(repo)}{' · ' + _esc(ref) if ref else ''}</span></li>"
                    for what, repo, ref in q.shipped
                )
                + "</ul></div>"
                for q in shipped
            )
            parts.append(
                "<h3>What shipped each quarter</h3><p class='anote'>Merged pull requests, largest first (by "
                "lines changed, or by number of commits when the size isn't recorded). A quarter without any "
                "lists the commits that describe features, improvements or fixes.</p>"
                f"<div class='shipped'>{blocks}</div>"
            )

    t = a.timeline
    if t is not None and len(t.rows) >= 2:
        hidden = (
            f" {t.hidden:,} more repositor{'y' if t.hidden == 1 else 'ies'} with fewer commits "
            f"{'is' if t.hidden == 1 else 'are'} left off."
            if t.hidden else ""
        )
        parts.append(
            "<div class='tlwrap'><h3>Project timeline</h3>"
            "<p class='anote'>When each repository was worked on, in the order the work began: a line from "
            "its first to its latest commit in this report, and a block for every month with commits "
            f"(darker = more commits).{hidden}</p>{_timeline(t)}</div>"
        )

    r = a.rhythm
    if r is not None:
        zone = scope.timezone if scope is not None else "UTC"
        cards = [
            (f"{r.active_weeks} of {r.weeks_in_span}", f"weeks with commits ({r.consistency:.0%})"),
            (f"{r.best_30_commits:,}",
             f"commits in the best 30 days ({r.best_30_start:%d %b} – {r.best_30_end:%d %b %Y})"),
            (f"{r.peak_start:02d}:00–{(r.peak_start + 3) % 24:02d}:00", f"busiest hours ({r.peak_share:.0%} of commits)"),
            (f"{r.weekend_share:.0%}", "of commits on weekends"),
        ]
        weeks = ""
        if r.span_start and r.span_end:
            span = f"{r.span_start:%d %b %Y} – {r.span_end:%d %b %Y}"
            weeks = (
                f"<p class='anote'>Weeks are counted across the report period, as far as it has run: {span}.</p>"
                if r.span_is_period else
                f"<p class='anote'>Weeks are counted from the first commit to the latest: {span}.</p>"
            )
        parts.append(
            "<h3>Working rhythm</h3>" + _analytics_cards(cards) + weeks
            + f"<div class='atable'><h4>Commits by day and hour ({_esc(zone)})</h4>{_heatmap(r)}</div>"
        )

    f = a.footprint
    if f is not None:
        cards = [
            (f"{f.median_lines:,.0f}", "median lines changed per commit"),
            (f"{f.small_share:.0%}", f"of commits change under {SMALL_COMMIT_LINES} lines"),
            (f"{f.p90_lines:,.0f}", "lines or fewer in 9 of 10 commits"),
        ]
        if f.removed_per_100_added is not None:
            cards.append((f"{f.removed_per_100_added:,.0f}", "lines removed per 100 added"))
        parts.append(
            "<h3>Code footprint</h3>" + _analytics_cards(cards)
            + f"<p class='anote'>From {f.measured:,} commits with line counts (merge commits left out). Line "
            "counts include generated and vendored files committed to the repositories (lock files, builds, "
            "assets), which can make totals large; the medians are barely affected.</p>"
        )
    parts.append("</section>")
    return "".join(parts)


def _markdown_analytics(a: Analytics | None, scope: ReportScope | None = None) -> list[str]:
    """The 'Insights & analytics' section of report.md."""
    if a is None or not (a.work_mix.total or a.work_mix.merges or a.delivery or a.reviews):
        return []
    out = ["## Insights & analytics\n"]
    if a.highlights:
        out.append("**Highlights:**\n")
        out.extend(f"- {line}" for line in a.highlights)
        out.append("")
    d = a.delivery
    if d is not None:
        out.append("### Delivery\n")
        out.append(f"- **Pull requests merged:** {d.merged:,} of {d.opened:,} opened")
        if d.merge_rate is not None:
            out.append(f"- **Closed pull requests merged:** {d.merge_rate:.0%}")
        if d.median_hours is not None:
            out.append(f"- **Median time to merge:** {human_duration(d.median_hours)} "
                       f"({d.within_day / len(d.hours_to_merge):.0%} within a day)")
        if d.median_size is not None:
            out.append(f"- **Median lines per merged pull request:** {d.median_size:,.0f}")
        out.append("")
        for title, buckets in (("Time to merge", d.time_buckets), ("Size of merged pull requests", d.size_buckets)):
            if buckets:
                total = sum(n for _b, n in buckets) or 1
                out.append(_md_table(pd.DataFrame(
                    [(b, n, f"{n / total:.0%}") for b, n in buckets], columns=[title, "pull requests", "share"]
                )))
                out.append("")
        if d.biggest:
            out.append("**Largest merged pull requests:**\n")
            out.append(_md_table(pd.DataFrame(
                [(clean_title(p.title), p.full_name, f"+{p.additions or 0:,} / −{p.deletions or 0:,}",
                  p.changed_files or 0) for p in d.biggest],
                columns=["pull request", "repository", "lines", "files"],
            )))
            out.append("")
    rv = a.reviews
    if rv is not None:
        out.append("### Code reviews\n")
        out.append(f"- **Code reviews given:** {rv.total:,} on {rv.pull_requests:,} pull requests by "
                   f"{rv.authors:,} teammate(s), in {len(rv.repositories):,} repositories")
        out.append("- **Outcomes:** " + ", ".join(f"{label} {n:,}" for label, n in rv.states))
        out.append("- **Reviewed most:** " + ", ".join(f"{name} ({n:,})" for name, n in rv.repositories[:5]))
        out.append("")
    mix = a.work_mix
    if mix.total:
        out.append("### Work mix\n")
        out.append(_md_table(pd.DataFrame(
            [(kind, mix.counts[kind], f"{mix.share(kind):.0%}") for kind in WORK_TYPES if mix.counts[kind]],
            columns=["kind of work", "commits", "share"],
        )))
        out.append("\n_Estimated from commit message wording; merge commits left out._\n")
    if a.quarters:
        out.append("### Quarter by quarter\n")
        quarters = pd.DataFrame(
            [
                (q.label + (" (partial)" if q.partial else ""), q.commits,
                 f"{q.change:+.0%}" if q.change is not None else "—", q.prs_merged, q.reviews,
                 q.repositories, q.active_days, f"+{q.additions:,} / −{q.deletions:,}")
                for q in a.quarters
            ],
            columns=["quarter", "commits", "vs previous", "PRs merged", "reviews", "repos", "active days", "lines"],
        )
        if not any(q.additions or q.deletions for q in a.quarters):
            quarters = quarters.drop(columns=["lines"])  # no line counts were collected
        if not any(q.reviews for q in a.quarters):
            quarters = quarters.drop(columns=["reviews"])  # no code reviews were collected
        out.append(_md_table(quarters))
        out.append("")
        for q in a.quarters:
            if q.shipped:
                out.append(f"**{q.label} — what shipped:**\n")
                out.extend(f"- {what} _({repo}{', ' + ref if ref else ''})_" for what, repo, ref in q.shipped)
                out.append("")
    t = a.timeline
    if t is not None and len(t.rows) >= 2:
        out.append("### Project timeline\n")
        out.append(_md_table(pd.DataFrame(
            [(row.repository, row.commits, f"{row.first:%d %b %Y}", f"{row.last:%d %b %Y}", len(row.by_month))
             for row in t.rows],
            columns=["repository", "commits", "first commit", "latest commit", "months with commits"],
        )))
        if t.hidden:
            out.append(f"\n_{t.hidden:,} more repositories with fewer commits are left off._")
        out.append("")
    r = a.rhythm
    if r is not None:
        zone = scope.timezone if scope is not None else "UTC"
        out.append("### Working rhythm\n")
        span = f", {r.span_start:%d %b %Y} – {r.span_end:%d %b %Y}" if r.span_start and r.span_end else ""
        out.append(f"- **Weeks with commits:** {r.active_weeks} of {r.weeks_in_span} ({r.consistency:.0%}{span})")
        out.append(f"- **Best 30 days:** {r.best_30_commits:,} commits "
                   f"({r.best_30_start:%d %b %Y} – {r.best_30_end:%d %b %Y})")
        out.append(f"- **Busiest hours ({zone}):** {r.peak_start:02d}:00–{(r.peak_start + 3) % 24:02d}:00 "
                   f"({r.peak_share:.0%} of commits)")
        out.append(f"- **Commits on weekends:** {r.weekend_share:.0%}")
        out.append("")
    f = a.footprint
    if f is not None:
        out.append("### Code footprint\n")
        out.append(f"- **Median lines changed per commit:** {f.median_lines:,.0f} "
                   f"({f.small_share:.0%} of commits under {SMALL_COMMIT_LINES} lines)")
        if f.removed_per_100_added is not None:
            out.append(f"- **Lines removed per 100 added:** {f.removed_per_100_added:,.0f}")
        out.append("")
    return out


def _render_exec_summary(exec_summary: ExecSummary) -> str:
    """The promotion-ready 'Executive summary' section."""
    parts: list[str] = [
        "<section id='exec-summary'><h2>Executive summary — contribution highlights</h2>",
        "<div class='exec'>",
    ]
    for paragraph in exec_summary.overview:
        parts.append(f"<p class='lead'>{_esc(paragraph)}</p>")

    if exec_summary.key_projects:
        parts.append("<h3>Key projects &amp; impact</h3>")
        for w in exec_summary.key_projects:
            lang = f" · {_esc(w.language)}" if w.language else ""
            top_work = "; ".join(w.highlights[:3])
            work_html = f"<div class='pwork'>Key work: {_esc(top_work)}</div>" if top_work else ""
            parts.append(
                "<div class='proj'>"
                f"<div class='pname'>{_esc(w.full_name)}</div>"
                f"<div class='pmeta'>{_esc(w.headline())}{lang}</div>"
                f"{work_html}</div>"
            )

    if exec_summary.features:
        parts.append("<h3>Features &amp; improvements delivered</h3><ul class='features'>")
        for repo, feature in exec_summary.features:
            parts.append(f"<li>{_esc(feature)} <span class='frepo'>— {_esc(repo)}</span></li>")
        parts.append("</ul>")

    if exec_summary.strengths:
        parts.append("<h3>Profile strengths</h3><ul class='features'>")
        for label, value in exec_summary.strengths:
            parts.append(f"<li><strong>{_esc(label)}:</strong> {_esc(value)}</li>")
        parts.append("</ul>")

    parts.append(
        "<p class='note'>Auto-generated from the collected commit and pull-request "
        "history — every figure above is verifiable in the tables below.</p>"
    )
    parts.append("</div></section>")
    return "".join(parts)


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
    scope: ReportScope | None = None,
) -> str:
    ins = insights.to_summary_dict()
    exec_summary = build_exec_summary(insights, summary)
    commits_by_repo = _commits_by_repo(stats.commits)
    parts: list[str] = []

    parts.append("<header class='hero'><div class='wrap'>")
    parts.append("<h1>GitHub Contribution Report</h1>")
    parts.append(
        f"<div class='meta'>Users: <strong>{_esc(summary.get('tracked_users'))}</strong> · "
        f"Period: <strong>{_esc(period_text(scope))}</strong> · "
        f"Activity {_esc(_fmt_date(summary.get('first_contribution_date')))} → "
        f"{_esc(_fmt_date(summary.get('latest_contribution_date')))} · "
        f"Generated {_esc(_fmt_date(summary.get('generated_at')))}</div>"
    )
    parts.append("</div></header>")

    parts.append("<div class='wrap'>")

    # Metric cards
    parts.append("<section><h2>At a glance</h2><div class='cards'>")
    for label, value in _metric_pairs(summary, insights, scope):
        parts.append(f"<div class='card'><div class='v'>{_esc(value)}</div><div class='l'>{_esc(label)}</div></div>")
    parts.append("</div></section>")

    # Gaps are shown up front: a reader must know before trusting the numbers.
    if scope is not None and scope.notes:
        parts.append(_render_completeness(scope))

    # Repository dashboard — the per-repo numbers, before any narrative.
    parts.append(_render_repo_dashboard(insights))

    # Executive summary (promotion-ready narrative)
    parts.append(_render_exec_summary(exec_summary))

    # Technologies, then insights & analytics (delivery, work mix, quarters, rhythm, footprint)
    parts.append(_render_stack(stats.analytics))
    parts.append(_render_analytics(stats.analytics, scope))

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
        "individual commit messages are terse. Click a repository's commit count "
        "to see every commit.</p>"
    )
    if commits_by_repo:
        parts.append(
            "<p class='noprint'><button type='button' class='toggle' id='toggle-commits' "
            "data-state='closed'>Expand all commit lists</button></p>"
        )
    for work in insights.repo_work:
        parts.append(_render_repo_card(work, commits_by_repo.get(work.full_name, [])))
    parts.append("</section>")

    if scope is not None and not scope.notes:
        parts.append(_render_completeness(scope))
    parts.append(_render_method(scope))

    # Appendix: the raw commit data, last, in chronological order.
    parts.append(_render_commit_timeline(stats.commits))

    parts.append("<footer>Generated by github-contrib · deterministic summary (no AI)</footer>")
    parts.append("</div>")

    body = "\n".join(parts)
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<meta http-equiv='Content-Security-Policy' content=\"{REPORT_CSP}\">"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>GitHub Contribution Report</title>"
        f"<style>{_CSS}</style></head><body>{body}"
        f"<script>{_SCRIPT}</script></body></html>"
    )


def _render_repo_card(work: RepoWork, commit_rows: list[dict] | None = None) -> str:
    badge = "private" if work.is_private else "public"
    lang = f" · {_esc(work.language)}" if work.language else ""
    chips = "".join(f"<span class='chip'>{_esc(t)}</span>" for t in work.themes)
    items = "".join(f"<li>{_esc(h)}</li>" for h in work.highlights)
    items_html = f"<ul>{items}</ul>" if items else "<p class='note'>No PR/branch descriptions available.</p>"
    themes_html = f"<div class='themes'>Recurring themes: {chips}</div>" if chips else ""

    impact_html = ""
    if work.total_additions or work.total_deletions:
        net = work.total_additions - work.total_deletions
        net_sign = "+" if net >= 0 else ""
        net_color = "#1e7d34" if net >= 0 else "#b3261e"
        impact_html = (
            "<div class='impact'>"
            f"<span class='istat add'>+{work.total_additions:,} added</span>"
            f"<span class='istat del'>−{work.total_deletions:,} deleted</span>"
            f"<span class='istat net' style='color:{net_color}'>{net_sign}{net:,} net</span>"
            f"<span class='istat files'>{work.total_files_changed:,} files</span>"
            "</div>"
        )
        if work.most_impactful_commit:
            impact_html += (
                f"<div class='bigcommit'>Largest commit: <em>{_esc(work.most_impactful_commit)}</em></div>"
            )

    commits_html = _render_commit_list(commit_rows or [])
    stack_html = (
        "<div class='stackline'>Stack: "
        + "".join(f"<span class='chip'>{_esc(t)}</span>" for t in work.technologies)
        + "</div>"
    ) if work.technologies else ""

    return (
        "<div class='repo'>"
        f"<div class='name'>{_esc(work.full_name)}<span class='badge {badge}'>{badge}</span></div>"
        f"<div class='sub'>{_esc(work.headline())}{lang}</div>"
        f"{stack_html}{impact_html}{items_html}{themes_html}{commits_html}"
        "</div>"
    )


def export_reports(
    output_dir: Path,
    stats: Statistics,
    insights: Insights,
    summary: dict[str, object],
    charts_dir: Path,
    include_charts: bool = True,
    scope: ReportScope | None = None,
) -> list[Path]:
    """Write report.md and report.html. Returns the paths written."""
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / "report.md"
    html_path = output_dir / "report.html"
    md_path.write_text(render_markdown(stats, insights, summary, scope), encoding="utf-8", newline="\n")
    html_path.write_text(
        render_html(stats, insights, summary, charts_dir, include_charts, scope),
        encoding="utf-8",
        newline="\n",
    )
    log.info("wrote %s and %s", md_path.name, html_path.name)
    return [md_path, html_path]
