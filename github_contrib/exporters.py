"""CSV, Excel and text-report exporters.

Commit messages, pull-request titles, branch names and repository descriptions
are text other people can write. A spreadsheet runs any cell starting with
``=``, ``+``, ``-`` or ``@`` as a formula (CSV / formula injection), so text
cells starting with those characters are neutralized: CSV cells get a leading
``'`` (removed again by the ``--regen`` loader) and Excel cells are stored as
plain text.
"""

from __future__ import annotations

import csv
import itertools
import os
import re
from pathlib import Path

import pandas as pd

from .logging_config import get_logger
from .statistics import CommitTable, Statistics

log = get_logger("exporters")

# Sheet name -> attribute on Statistics. The first seven are the required
# sheets (in the required order); the rest are useful extras.
_EXCEL_SHEETS: list[tuple[str, str]] = [
    ("Commits", "commits"),
    ("Pull Requests", "pull_requests"),
    ("Repositories", "repositories"),
    ("Organizations", "organizations"),
    ("Summary", "summary"),
    ("Yearly Stats", "commits_per_year"),
    ("Monthly Stats", "commits_per_month"),
    ("Per User", "per_user"),
    ("Commits Per Repo", "commits_per_repo"),
    ("By Organization", "commits_by_org"),
    ("By Email", "commits_by_email"),
    ("Top Repositories", "top_repositories"),
    ("PR Summary", "pr_summary"),
    ("Lines By Repo", "lines_by_repo"),
    ("Top Commits Impact", "top_commits_by_impact"),
    # Insights & analytics (analytics.py)
    ("Highlights", "highlights"),
    ("Quarterly", "quarterly"),
    ("Work Mix", "work_mix"),
    ("PR Delivery", "pr_delivery"),
    ("Working Rhythm", "working_rhythm"),
    ("Technologies", "technologies"),
    ("Code Reviews", "reviews"),
]

# CSV file name -> attribute on Statistics.
_CSV_FILES: list[tuple[str, str]] = [
    ("commits.csv", "commits"),
    ("pull_requests.csv", "pull_requests"),
    ("repositories.csv", "repositories"),
    ("organizations.csv", "organizations"),
    ("reviews.csv", "reviews"),
    ("contribution_summary.csv", "summary"),
]


#: First characters that make a spreadsheet treat a cell as a formula.
FORMULA_PREFIXES = ("=", "+", "-", "@", chr(9), chr(13))  # chr(9)/chr(13): tab, carriage return
_NUMBER_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$")


def _neutralize(value: object) -> object:
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES) and not _NUMBER_RE.match(value):
        return "'" + value
    return value


def neutralize_formulas(df: pd.DataFrame) -> pd.DataFrame:
    """A copy of ``df`` whose text cells cannot run as spreadsheet formulas."""
    # pandas 3 stores text with the "str" dtype; older data uses object.
    text_columns = [
        c for c in df.columns
        if pd.api.types.is_object_dtype(df[c].dtype) or pd.api.types.is_string_dtype(df[c].dtype)
    ]
    if not text_columns:
        return df
    out = df.copy()
    for column in text_columns:
        out[column] = out[column].map(_neutralize)
    return out


#: The CSV that is a result of the report, not collected data: rewritten by
#: ``--regen`` too, so it always matches the report next to it.
SUMMARY_CSV = "contribution_summary.csv"


def export_csvs(output_dir: Path, stats: Statistics, *, only: tuple[str, ...] | None = None) -> list[Path]:
    """Write the required CSV files (``only`` those names, when given).
    Returns the paths written."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, attr in _CSV_FILES:
        if only is not None and filename not in only:
            continue
        table = getattr(stats, attr)
        path = output_dir / filename
        if isinstance(table, CommitTable):
            _stream_csv(path, table)
        else:
            neutralize_formulas(table).to_csv(path, index=False, encoding="utf-8-sig")
        written.append(path)
        log.info("wrote %s (%d rows)", path.name, len(table))
    return written


def _csv_value(value: object) -> object:
    """A value as pandas writes it to CSV (missing -> empty, text neutralized)."""
    if value is None:
        return ""
    if isinstance(value, float) and value != value:
        return ""
    if isinstance(value, str):
        return _neutralize(value)
    return value


def _stream_csv(path: Path, table: CommitTable) -> None:
    """Every commit to CSV as it is read - the same bytes pandas would write
    (UTF-8 with a BOM, minimal quoting, the platform's line ending) - via a
    temporary file renamed into place."""
    partial = path.with_name(path.name + ".partial")
    try:
        with partial.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh, lineterminator=os.linesep)
            writer.writerow(table.columns)
            for row in table.rows():
                writer.writerow([_csv_value(value) for value in row])
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)


#: Characters a worksheet cannot hold (openpyxl refuses them: a single commit
#: message with a terminal colour code would otherwise stop the whole report).
_ILLEGAL_XLSX_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
#: Longest text an Excel cell holds.
_XLSX_CELL_LIMIT = 32767
#: Rows sampled to size each column.
_WIDTH_SAMPLE = 200
#: Data rows per worksheet (Excel's limit is 1,048,576 rows, header included).
_XLSX_DATA_ROWS = 1_048_575


def _excel_value(value: object) -> object:
    """A pandas cell as a value openpyxl writes: missing -> empty, numpy ->
    Python, text cleaned of characters a worksheet can't hold and cut to
    Excel's cell limit."""
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()  # numpy scalar -> Python
        except (ValueError, AttributeError):
            pass
    if isinstance(value, (list, tuple, set, dict)):
        value = str(value)
    if isinstance(value, str):
        value = _ILLEGAL_XLSX_RE.sub("�", value)
        if len(value) > _XLSX_CELL_LIMIT:
            value = value[: _XLSX_CELL_LIMIT - 1] + "…"
    return value


def export_excel(output_dir: Path, stats: Statistics, filename: str = "github_contributions.xlsx") -> Path:
    """Write a formatted multi-sheet Excel workbook.

    Written row by row (openpyxl's write-only mode), so memory stays flat
    however many commits the report holds: building the whole workbook first
    took over 200 MB for 30,000 commits. Text is always stored as text (never
    as a formula), and the file appears only once complete."""
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / filename
    partial = path.with_name(path.name + ".partial")
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="305496")
    header_align = Alignment(vertical="center", horizontal="left")

    workbook = Workbook(write_only=True)
    try:
        for sheet_name, attr in _EXCEL_SHEETS:
            table = getattr(stats, attr)
            if isinstance(table, CommitTable):
                columns, total = list(table.columns), len(table)
                sample = [list(row) for row in itertools.islice(table.rows(), _WIDTH_SAMPLE)]
                rows = table.rows()
            else:
                columns, total = [str(c) for c in table.columns], len(table)
                sample = [list(row) for row in table.iloc[:_WIDTH_SAMPLE].itertuples(index=False, name=None)]
                rows = table.itertuples(index=False, name=None)
            if not columns:
                workbook.create_sheet(_safe_sheet_name(sheet_name))
                continue
            # Approximate auto widths from the header and the first rows.
            widths = []
            for index, column in enumerate(columns):
                longest = len(column)
                for row in sample:
                    value = _excel_value(row[index])
                    if value is not None:
                        longest = max(longest, len(str(value)))
                widths.append(min(max(longest + 2, 10), 60))

            def new_sheet(part: int, rows_in_it: int):
                # Excel sheet names are capped at 31 chars and have reserved
                # chars; a table longer than a sheet continues on the next one.
                name = sheet_name if part == 1 else f"{sheet_name} ({part})"
                sheet = workbook.create_sheet(_safe_sheet_name(name))
                for index, width in enumerate(widths, start=1):
                    sheet.column_dimensions[get_column_letter(index)].width = width
                sheet.freeze_panes = "A2"
                sheet.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{rows_in_it + 1}"
                header = []
                for column in columns:
                    cell = WriteOnlyCell(sheet, value=_excel_value(column))
                    cell.font, cell.fill, cell.alignment = header_font, header_fill, header_align
                    header.append(cell)
                sheet.append(header)
                return sheet

            part, written_in_sheet = 1, 0
            sheet = new_sheet(1, min(total, _XLSX_DATA_ROWS))
            for row in rows:
                if written_in_sheet == _XLSX_DATA_ROWS:
                    part, written_in_sheet = part + 1, 0
                    sheet = new_sheet(part, min(total - (part - 1) * _XLSX_DATA_ROWS, _XLSX_DATA_ROWS))
                cells = []
                for value in row:
                    value = _excel_value(value)
                    if isinstance(value, str) and value.startswith("="):
                        # openpyxl stores text starting with "=" as a live
                        # formula; the workbook never holds formulas.
                        cell = WriteOnlyCell(sheet, value=value)
                        cell.data_type = "s"
                        cells.append(cell)
                    else:
                        cells.append(value)
                sheet.append(cells)
                written_in_sheet += 1
        workbook.save(partial)
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)
    log.info("wrote %s (%d sheets)", path.name, len(_EXCEL_SHEETS))
    return path


def export_summary_report(output_dir: Path, stats: Statistics, filename: str = "summary_report.txt") -> Path:
    """Write a human-readable plain-text summary report."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / filename
    lines: list[str] = []
    lines.append("=" * 64)
    lines.append("GITHUB CONTRIBUTION SUMMARY REPORT")
    lines.append("=" * 64)
    lines.append("")
    period = stats.summary_dict.get("report_period")
    for key, value in stats.summary_dict.items():
        label = key.replace("_", " ").title()
        if key == "total_lifetime_commits" and period and period != "all time":
            label = "Total Commits (Period)"
        lines.append(f"{label:<34}: {value}")
    lines.append("")

    if not stats.per_user.empty:
        lines.append("-" * 64)
        lines.append("PER-USER BREAKDOWN")
        lines.append("-" * 64)
        lines.append(stats.per_user.to_string(index=False))
        lines.append("")

    if not stats.top_repositories.empty:
        lines.append("-" * 64)
        lines.append("TOP REPOSITORIES BY COMMITS")
        lines.append("-" * 64)
        lines.append(stats.top_repositories.to_string(index=False))
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("wrote %s", path.name)
    return path


# ---------------------------------------------------------------------------
# Excel formatting helpers
# ---------------------------------------------------------------------------

def _safe_sheet_name(name: str) -> str:
    invalid = set(r"[]:*?/\\")
    cleaned = "".join("_" if ch in invalid else ch for ch in name)
    return cleaned[:31]
