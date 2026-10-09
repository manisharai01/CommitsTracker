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
import hashlib
import html
from datetime import datetime
from pathlib import Path

import pandas as pd

from .filters import ReportScope
from .insights import ExecSummary, Insights, RepoWork, build_exec_summary
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
/* Commit timeline appendix */
#commit-timeline tr.month th { background:#eef2f9; color:#33476a; font-size:12.5px; letter-spacing:.04em;
                               text-transform:uppercase; print-color-adjust:exact; -webkit-print-color-adjust:exact; }
#commit-timeline td.cnum { color:var(--muted); text-align:right; white-space:nowrap; }
/* Print / PDF */
@media print {
  body { background:#fff; }
  .wrap { max-width:none; padding:0 4px; }
  .card, .charts figure, .exec .proj, tr { break-inside:avoid; }
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

_SCRIPT = """
(function () {
  function setAll(open, selector) {
    document.querySelectorAll(selector || 'details.commitlist').forEach(function (d) { d.open = open; });
  }
  var btn = document.getElementById('toggle-commits');
  if (btn) {
    btn.addEventListener('click', function () {
      var open = btn.dataset.state !== 'open';
      setAll(open);
      btn.dataset.state = open ? 'open' : 'closed';
      btn.textContent = open ? 'Collapse all commit lists' : 'Expand all commit lists';
    });
  }
  // A PDF prints only one part of a long appendix at a time: its rows FROM
  // to TO-1, after the rest of the report (first part) or alone (later ones).
  function printPart(from, to) {
    var rows = document.querySelectorAll('#commit-timeline tbody > tr');
    for (var i = 0; i < rows.length; i++) {
      if (i < from || i >= to) rows[i].classList.add('pskip');
    }
    // A part that starts mid-month repeats that month's header.
    var first = rows[from];
    if (from > 0 && first && !first.classList.contains('month')) {
      for (var j = from - 1; j >= 0; j--) {
        if (rows[j].classList.contains('month')) {
          var head = rows[j].cloneNode(true);
          head.classList.remove('pskip');
          head.cells[0].textContent += ' (continued)';
          first.parentNode.insertBefore(head, first);
          break;
        }
      }
    }
    if (from > 0) document.body.classList.add('pcont');
    if (to < rows.length) document.body.classList.add('pmore');
  }
  // For PDF/printing only the chronological appendix is expanded; collapsed
  // per-repo lists are hidden by the print stylesheet so raw commit data
  // appears once, at the end, in time order ('#print', or '#print=FROM-TO'
  // for one part of it).
  var print = /^#print(?:=(\\d+)-(\\d+))?$/.exec(location.hash);
  if (print) {
    setAll(true, 'details.timeline');
    if (print[1]) printPart(+print[1], +print[2]);
  }
  window.addEventListener('beforeprint', function () { setAll(true, 'details.timeline'); });
})();
"""

#: The only script the report may run (anything injected would not match).
SCRIPT_HASH = "sha256-" + base64.b64encode(hashlib.sha256(_SCRIPT.encode("utf-8")).digest()).decode("ascii")
#: Content-Security-Policy of report.html: no network access at all (charts
#: are data URIs), inline styles, and only the script above.
REPORT_CSP = (
    "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
    f"script-src '{SCRIPT_HASH}'; base-uri 'none'; form-action 'none'"
)


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
            span = f"{w.first_activity.date()} → {w.last_activity.date()}"
        rows.append(
            "<tr>"
            f"<td class='dname'>{_esc(w.full_name)}</td>"
            f"<td class='dnum'>{w.commits:,}</td>"
            f"<td class='dbarcell'><div class='dbar' style='width:{width:.1f}%'></div></td>"
            f"<td class='dshare'>{share:.1f}%</td>"
            f"<td class='dmeta'>{_esc(pr)}</td>"
            f"<td class='dmeta'>{_esc(w.language or '—')}</td>"
            f"<td class='dmeta'>{_esc(span)}</td>"
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

    return (
        "<div class='repo'>"
        f"<div class='name'>{_esc(work.full_name)}<span class='badge {badge}'>{badge}</span></div>"
        f"<div class='sub'>{_esc(work.headline())}{lang}</div>"
        f"{impact_html}{items_html}{themes_html}{commits_html}"
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
