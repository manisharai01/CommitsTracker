"""The streaming .xlsx writer: values, text safety, sheet splitting and a
package every reader opens.

Run directly:

    python tests/test_xlsx.py

or with pytest:

    pytest -q tests
"""

from __future__ import annotations

import sys
import tempfile
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from openpyxl import load_workbook  # noqa: E402

from github_contrib import xlsx  # noqa: E402
from github_contrib.xlsx import CELL_LIMIT, XlsxWriter, column_letter  # noqa: E402


def _write(tmp: Path, sheets) -> Path:
    path = tmp / "book.xlsx"
    with XlsxWriter(path) as book:
        for name, columns, rows in sheets:
            book.add_sheet(name, columns, rows, [12] * len(columns))
    return path


def test_values_come_back_as_written():
    rows = [
        ["text", 1, 2.5, True, None, ""],
        [" padded ", np.int64(7), np.float64(0.1 + 0.2), np.bool_(False), float("nan"), "a & <b> \"c\" 'd'"],
        ["=1+1", -3, float("inf"), False, pd.NA, "@SUM(A1)"],
        [pd.Timestamp("2026-01-02T03:04:05Z"), Decimal("1.25"), date(2026, 5, 1), datetime(2026, 5, 1, 9, 30), pd.NaT, "x"],
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(Path(tmp), [("Data", ["a", "b", "c", "d", "e", "f"], rows)])
        sheet = load_workbook(path)["Data"]
        got = [[cell.value for cell in row] for row in sheet.iter_rows(min_row=2)]
        assert got[0] == ["text", 1, 2.5, True, None, None]
        assert got[1][:4] == [" padded ", 7, 0.3, False] and got[1][4] is None
        assert got[1][5] == "a & <b> \"c\" 'd'"
        assert got[2][:2] == ["=1+1", -3] and got[2][2] is None and got[2][5] == "@SUM(A1)"
        assert all(cell.data_type == "s" for cell in sheet[4] if isinstance(cell.value, str)), "text, never a formula"
        assert got[3][0] == "2026-01-02T03:04:05+00:00" and got[3][1] == 1.25
        assert got[3][2] == "2026-05-01" and got[3][3] == "2026-05-01T09:30:00"
        header = [cell for cell in sheet[1]]
        assert [c.value for c in header] == ["a", "b", "c", "d", "e", "f"]
        assert header[0].font.b and header[0].fill.fgColor.rgb.endswith("305496")
        assert sheet.freeze_panes == "A2" and sheet.auto_filter.ref == "A1:F5"
        assert sheet.column_dimensions["A"].width == 12


def test_text_a_worksheet_cannot_hold_is_cleaned():
    long = "y" * (CELL_LIMIT + 50)
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(Path(tmp), [("T", ["v"], [["bell\x07 esc\x1b[31m"], [long], ["half \ud800 surrogate"], ["tab\tand\nline"]])])
        book = load_workbook(path, read_only=True)
        values = [row[0] for row in book["T"].iter_rows(min_row=2, values_only=True)]
        book.close()
        assert values[0] == "bell� esc�[31m"
        assert len(values[1]) == CELL_LIMIT and values[1].endswith("…")
        assert values[2] == "half ? surrogate", "a lone surrogate no longer stops the workbook"
        assert values[3] == "tab\tand\nline"


def test_long_tables_continue_on_numbered_sheets():
    original = xlsx.MAX_DATA_ROWS
    xlsx.MAX_DATA_ROWS = 3
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), [
                ("Commits", ["n"], ([i] for i in range(7))),
                ("Exact", ["n"], ([i] for i in range(3))),
                ("Empty", [], []),
                ("No rows", ["a", "b"], []),
            ])
            book = load_workbook(path)
            assert book.sheetnames == ["Commits", "Commits (2)", "Commits (3)", "Exact", "Empty", "No rows"]
            assert [r[0] for r in book["Commits (3)"].iter_rows(min_row=2, values_only=True)] == [6]
            assert book["Commits (2)"].auto_filter.ref == "A1:A4" and book["Commits (3)"].auto_filter.ref == "A1:A2"
            assert book["No rows"].auto_filter.ref == "A1:B1" and book["Empty"].max_row == 1
    finally:
        xlsx.MAX_DATA_ROWS = original


def test_sheets_stay_under_the_zip_entry_limit():
    original = xlsx._PART_BYTES
    xlsx._PART_BYTES = 2000
    try:
        with tempfile.TemporaryDirectory() as tmp:
            rows = [[f"row {i} " + "z" * 300] for i in range(40)]
            path = _write(Path(tmp), [("Big", ["text"], rows)])
            book = load_workbook(path, read_only=True)
            assert len(book.sheetnames) > 2 and book.sheetnames[1] == "Big (2)"
            back = [r[0] for ws in book.worksheets for r in ws.iter_rows(min_row=2, values_only=True)]
            book.close()
            assert back == [r[0] for r in rows], "every row once, in order"
    finally:
        xlsx._PART_BYTES = original


def test_the_package_is_well_formed():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(Path(tmp), [("O'Neil & co [x]", ["a"], [["1"]]), ("Plain", ["a"], [])])
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
            for name in archive.namelist():
                ElementTree.fromstring(archive.read(name))  # raises on bad XML
            workbook = archive.read("xl/workbook.xml").decode()
            assert "'O''Neil &amp; co _x_'!$A$1:$A$2" in workbook
            assert all(info.extract_version < 45 for info in archive.infolist()), "no 64-bit zip entries"
        assert load_workbook(path).sheetnames == ["O'Neil & co _x_", "Plain"]
        frame = pd.read_excel(path, sheet_name="O'Neil & co _x_")
        assert list(frame.columns) == ["a"] and frame.iloc[0, 0] == 1 or frame.iloc[0, 0] == "1"


def test_column_letters():
    assert [column_letter(n) for n in (1, 26, 27, 52, 53, 702, 703, 16384)] == [
        "A", "Z", "AA", "AZ", "BA", "ZZ", "AAA", "XFD"]


def test_a_failed_write_leaves_no_workbook():
    from github_contrib.exporters import export_excel
    from github_contrib.statistics import compute_statistics
    from github_contrib.models import CollectedData

    stats = compute_statistics(CollectedData())

    def broken_rows():
        yield ("ok",)
        raise RuntimeError("disk full")

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        stats.repositories = pd.DataFrame({"name": ["a"]})
        stats.repositories.itertuples = lambda index=False, name=None: broken_rows()
        try:
            export_excel(out, stats)
        except RuntimeError:
            pass
        else:
            raise AssertionError("the error reaches the caller")
        assert list(out.iterdir()) == [], "no partial workbook is left behind"


def _all_tests():
    return [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    for test in _all_tests():
        test()
        print("ok ", test.__name__)
