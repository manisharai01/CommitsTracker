"""A streaming .xlsx writer.

Rows go into the zip as they are read, as worksheet XML written directly, so a
sheet of a million rows needs no more memory than a sheet of ten, and writing
is about fifteen times faster than building openpyxl cell objects (which took
most of the time of a large report).

The package mirrors what openpyxl writes - the same parts, styles and sheet
layout - so a workbook looks the same as before: a bold header row, the first
row frozen, column widths, an autofilter on every sheet. Text is always
stored as text: the workbook holds no formulas.
"""

from __future__ import annotations

import math
import re
import zipfile
from datetime import date, datetime, time as clock, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Sequence

#: Data rows per worksheet: Excel's limit is 1,048,576 rows, header included.
#: A longer table continues on "<name> (2)".
MAX_DATA_ROWS = 1_048_575
#: Longest text an Excel cell holds.
CELL_LIMIT = 32767
#: Characters a worksheet cannot hold.
ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
#: Sheet XML past which the table goes on in the next sheet: a zip entry
#: past 2 GiB needs 64-bit sizes, which some spreadsheet programs refuse.
_PART_BYTES = 1536 * 2**20
#: Rows (or characters, for very long rows) joined into one write.
_CHUNK_ROWS = 1000
_CHUNK_CHARS = 2**20

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_TYPE = "application/vnd.openxmlformats-officedocument"

_STYLES = (
    f'<styleSheet xmlns="{_MAIN}"><numFmts count="0" />'
    '<fonts count="2"><font><name val="Calibri" /><family val="2" /><color theme="1" /><sz val="11" />'
    '<scheme val="minor" /></font><font><b val="1" /><color rgb="00FFFFFF" /></font></fonts>'
    '<fills count="3"><fill><patternFill /></fill><fill><patternFill patternType="gray125" /></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="00305496" /></patternFill></fill></fills>'
    '<borders count="1"><border><left /><right /><top /><bottom /><diagonal /></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" /></cellStyleXfs>'
    '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" pivotButton="0" quotePrefix="0" xfId="0" />'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" applyAlignment="1" pivotButton="0" quotePrefix="0" xfId="0">'
    '<alignment horizontal="left" vertical="center" /></xf></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0" hidden="0" /></cellStyles>'
    '<tableStyles count="0" defaultTableStyle="TableStyleMedium9" defaultPivotStyle="PivotStyleLight16" />'
    "</styleSheet>"
)
_SHEET_PR = '<sheetPr><outlinePr summaryBelow="1" summaryRight="1" /><pageSetUpPr /></sheetPr>'
_FROZEN_VIEW = (
    '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
    'state="frozen" /><selection pane="bottomLeft" activeCell="A1" sqref="A1" /></sheetView></sheetViews>'
)
_PLAIN_VIEW = '<sheetViews><sheetView workbookViewId="0"><selection activeCell="A1" sqref="A1" /></sheetView></sheetViews>'
_FORMAT_PR = '<sheetFormatPr baseColWidth="8" defaultRowHeight="15" />'
_MARGINS = '<pageMargins left="0.75" right="0.75" top="1" bottom="1" header="0.5" footer="0.5" />'


def column_letter(index: int) -> str:
    """The letters of a 1-based column number: 1 -> A, 27 -> AA."""
    letters = ""
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def sheet_title(name: str) -> str:
    """A name Excel accepts for a sheet: no reserved characters, 31 at most."""
    invalid = set(r"[]:*?/\\")
    return "".join("_" if ch in invalid else ch for ch in name)[:31]


def _escape(text: str) -> str:
    if "&" in text:
        text = text.replace("&", "&amp;")
    if "<" in text:
        text = text.replace("<", "&lt;")
    if ">" in text:
        text = text.replace(">", "&gt;")
    return text


def _attr(text: str) -> str:
    return _escape(text).replace('"', "&quot;")


def clean_text(text: str) -> str:
    """Text as a cell can hold it: control characters a worksheet refuses
    replaced and cut to Excel's cell limit. (Lone surrogate halves, which
    UTF-8 cannot hold, become "?" when the sheet is written.)"""
    text = ILLEGAL_RE.sub("\ufffd", text)
    if len(text) > CELL_LIMIT:
        text = text[: CELL_LIMIT - 1] + "\u2026"
    return text


def cell_value(value: object) -> object:
    """Any table value as one of: None (no cell), bool, a number or text."""
    if value is None:
        return None
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item") and not isinstance(value, bytes):
        try:
            return cell_value(value.item())  # numpy / pandas scalar -> Python
        except (ValueError, AttributeError, TypeError):
            pass
    try:
        import pandas as pd

        if value is pd.NA or value is pd.NaT:
            return None
    except ImportError:  # pragma: no cover
        pass
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, (datetime, date, clock)):
        return value.isoformat()
    return clean_text(str(value))


def _cell(ref: str, value: object, style: str = "") -> str:
    """One ``<c>`` element, or "" for no cell, as openpyxl writes it."""
    if value is None:
        return ""
    if isinstance(value, str):
        if not value:
            return f'<c r="{ref}"{style} t="inlineStr" />'
        space = ' xml:space="preserve"' if value != value.strip() else ""
        return f'<c r="{ref}"{style} t="inlineStr"><is><t{space}>{_escape(value)}</t></is></c>'
    if isinstance(value, bool):
        return f'<c r="{ref}"{style} t="b"><v>{1 if value else 0}</v></c>'
    return f'<c r="{ref}"{style} t="n"><v>{"%.16g" % value}</v></c>'


class XlsxWriter:
    """Writes a workbook sheet by sheet; the file is complete once closed.

    >>> with XlsxWriter(path) as book:
    ...     book.add_sheet("Commits", columns, rows, widths)
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._zip = zipfile.ZipFile(self.path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True)
        self._sheets: list[tuple[str, str | None]] = []  # (title, autofilter range or None)
        self._closed = False
        self._write("docProps/app.xml", (
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
            "<Application>Microsoft Excel Compatible</Application><AppVersion>3.1</AppVersion></Properties>"
        ))
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write("docProps/core.xml", (
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:creator>github_report</dc:creator>'
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
            f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified></cp:coreProperties>'
        ))
        self._write("xl/theme/theme1.xml", _theme())

    def __enter__(self) -> "XlsxWriter":
        return self

    def __exit__(self, kind, error, trace) -> None:
        if kind is None:
            self.close()
        else:
            self._zip.close()

    def _write(self, name: str, text: str) -> None:
        self._zip.writestr(name, text.encode("utf-8", "replace"))

    def add_sheet(
        self,
        name: str,
        columns: Sequence[str],
        rows: Iterable[Sequence[object]],
        widths: Sequence[float] = (),
    ) -> int:
        """A sheet with a header row of ``columns`` and then ``rows``. Past
        Excel's row limit (or 1.5 GB of sheet XML, so no zip entry needs
        64-bit sizes) the table continues on "<name> (2)", "<name> (3)"...
        With no columns, an empty sheet. Returns the data rows written."""
        title = sheet_title(name)
        if not columns:
            self._add_part(title, None, _Rows(()), ())
            return 0
        letters = [column_letter(i) for i in range(1, len(columns) + 1)]
        header = "".join(_cell(f"{letter}1", cell_value(column), ' s="1"') for letter, column in zip(letters, columns))
        pending = _Rows(rows)
        written, part = 0, 1
        while True:
            written += self._add_part(title, letters, pending, widths, header)
            if not pending.more():
                return written
            part += 1
            title = sheet_title(f"{name} ({part})")

    def _add_part(self, title: str, letters, rows: "_Rows", widths, header: str = "") -> int:
        """One worksheet: the header and rows until the table ends or the
        sheet is full. Returns the data rows written."""
        entry = f"xl/worksheets/sheet{len(self._sheets) + 1}.xml"
        written = size = 0
        with self._zip.open(entry, "w") as raw:
            def put(text: str) -> int:
                # The markup never holds a control character, so replacing
                # them here is the same as replacing them cell by cell.
                data = ILLEGAL_RE.sub("\ufffd", text).encode("utf-8", "replace")
                raw.write(data)
                return len(data)

            head = [f'<worksheet xmlns="{_MAIN}">', _SHEET_PR, _FROZEN_VIEW if letters else _PLAIN_VIEW, _FORMAT_PR]
            if letters and widths:
                head.append("<cols>")
                for number, width in enumerate(widths, start=1):
                    head.append(f'<col width="{_number(width)}" customWidth="1" min="{number}" max="{number}" />')
                head.append("</cols>")
            head.append("<sheetData>")
            if letters:
                head.append(f'<row r="1">{header}</row>')
            size += put("".join(head))
            if letters:
                chunk: list[str] = []
                held = 0  # characters in the chunk
                for row in rows:
                    written += 1
                    number = written + 1
                    cells = []
                    for letter, value in zip(letters, row):
                        if value is None:
                            continue
                        kind = type(value)
                        if kind is str:  # most cells: what cell_value and _cell do, inline
                            if not value:
                                cells.append(f'<c r="{letter}{number}" t="inlineStr" />')
                                continue
                            if len(value) > CELL_LIMIT:
                                value = value[: CELL_LIMIT - 1] + "\u2026"
                            space = ' xml:space="preserve"' if value[0].isspace() or value[-1].isspace() else ""
                            if "&" in value or "<" in value or ">" in value:
                                value = _escape(value)
                            # (Characters a sheet can't hold are replaced in put().)
                            cells.append(f'<c r="{letter}{number}" t="inlineStr"><is><t{space}>{value}</t></is></c>')
                        elif kind is int:
                            cells.append(f'<c r="{letter}{number}" t="n"><v>{"%.16g" % value}</v></c>')
                        else:
                            text = _cell(f"{letter}{number}", cell_value(value))
                            if text:
                                cells.append(text)
                    line = f'<row r="{number}">{"".join(cells)}</row>'
                    chunk.append(line)
                    held += len(line)
                    if len(chunk) >= _CHUNK_ROWS or held >= _CHUNK_CHARS:
                        size += put("".join(chunk))
                        chunk, held = [], 0
                    if written == MAX_DATA_ROWS or size + held >= _PART_BYTES:
                        break
                if chunk:
                    size += put("".join(chunk))
            tail = ["</sheetData>"]
            span = None
            if letters:
                span = f"A1:{letters[-1]}{written + 1}"
                tail.append(f'<autoFilter ref="{span}" />')
            tail.append(_MARGINS + "</worksheet>")
            put("".join(tail))
        self._sheets.append((title, span))
        return written

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            sheets = self._sheets
            self._write("xl/styles.xml", _STYLES)
            self._write("_rels/.rels", (
                f'<Relationships xmlns="{_PKG_REL}">'
                f'<Relationship Type="{_REL}/officeDocument" Target="xl/workbook.xml" Id="rId1" />'
                f'<Relationship Type="{_PKG_REL}/metadata/core-properties" Target="docProps/core.xml" Id="rId2" />'
                f'<Relationship Type="{_REL}/extended-properties" Target="docProps/app.xml" Id="rId3" />'
                "</Relationships>"
            ))
            listed = "".join(
                f'<sheet name="{_attr(title)}" sheetId="{i}" state="visible" r:id="rId{i}" />'
                for i, (title, _) in enumerate(sheets, start=1)
            )
            names = "".join(
                f'<definedName name="_xlnm._FilterDatabase" localSheetId="{i}" hidden="1">'
                f"{_escape(_quoted(title))}!{_absolute(span)}</definedName>"
                for i, (title, span) in enumerate(sheets)
                if span
            )
            self._write("xl/workbook.xml", (
                f'<workbook xmlns:r="{_REL}" xmlns="{_MAIN}"><workbookPr /><workbookProtection />'
                '<bookViews><workbookView visibility="visible" minimized="0" showHorizontalScroll="1" '
                'showVerticalScroll="1" showSheetTabs="1" tabRatio="600" firstSheet="0" activeTab="0" '
                'autoFilterDateGrouping="1" /></bookViews>'
                f"<sheets>{listed}</sheets>"
                + (f"<definedNames>{names}</definedNames>" if names else "")
                + '<calcPr calcId="124519" fullCalcOnLoad="1" /></workbook>'
            ))
            count = len(sheets)
            self._write("xl/_rels/workbook.xml.rels", (
                f'<Relationships xmlns="{_PKG_REL}">'
                + "".join(
                    f'<Relationship Type="{_REL}/worksheet" Target="/xl/worksheets/sheet{i}.xml" Id="rId{i}" />'
                    for i in range(1, count + 1)
                )
                + f'<Relationship Type="{_REL}/styles" Target="styles.xml" Id="rId{count + 1}" />'
                + f'<Relationship Type="{_REL}/theme" Target="theme/theme1.xml" Id="rId{count + 2}" />'
                + "</Relationships>"
            ))
            self._write("[Content_Types].xml", (
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml" />'
                '<Default Extension="xml" ContentType="application/xml" />'
                f'<Override PartName="/xl/styles.xml" ContentType="{_TYPE}.spreadsheetml.styles+xml" />'
                f'<Override PartName="/xl/theme/theme1.xml" ContentType="{_TYPE}.theme+xml" />'
                '<Override PartName="/docProps/core.xml" '
                'ContentType="application/vnd.openxmlformats-package.core-properties+xml" />'
                f'<Override PartName="/docProps/app.xml" ContentType="{_TYPE}.extended-properties+xml" />'
                + "".join(
                    f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                    f'ContentType="{_TYPE}.spreadsheetml.worksheet+xml" />'
                    for i in range(1, count + 1)
                )
                + f'<Override PartName="/xl/workbook.xml" ContentType="{_TYPE}.spreadsheetml.sheet.main+xml" />'
                "</Types>"
            ))
        finally:
            self._zip.close()


class _Rows:
    """An iterator of rows that can tell whether another row follows."""

    def __init__(self, rows: Iterable[Sequence[object]]) -> None:
        self._rows = iter(rows)
        self._held: list[Sequence[object]] = []

    def __iter__(self) -> "_Rows":
        return self

    def __next__(self) -> Sequence[object]:
        return self._held.pop() if self._held else next(self._rows)

    def more(self) -> bool:
        if not self._held:
            try:
                self._held.append(next(self._rows))
            except StopIteration:
                return False
        return True


def _number(value: float) -> str:
    return "%.16g" % value


def _quoted(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def _absolute(span: str) -> str:
    """A1:C10 -> $A$1:$C$10"""
    return ":".join(re.sub(r"^([A-Z]+)(\d+)$", r"$\1$\2", part) for part in span.split(":"))


def _theme() -> str:
    """The default Office theme the styles refer to (openpyxl's copy)."""
    from openpyxl.writer.theme import theme_xml

    return theme_xml


__all__ = ["CELL_LIMIT", "MAX_DATA_ROWS", "XlsxWriter", "cell_value", "clean_text", "column_letter", "sheet_title"]
