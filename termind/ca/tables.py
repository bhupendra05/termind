"""Shared table/text readers for the CA workbench — stdlib first, lazy parsers off-network.

bank.py reads bank statements; the other sections (scrutiny, gst, finstmt, notice) read ledgers,
invoice registers, trial balances and notices. They all need the same boring thing: turn a
CSV/Excel/PDF into a grid of cells (or, for notices, into text) — locally, with no upload. This
centralizes that so every section parses files the same way and reuses bank's "install the parser
into the workspace venv" message when a format needs a third-party reader.
"""
from __future__ import annotations

import csv
import io
import os

from .bank import StatementError, _need, _num, _parse_date

# re-export under clean names so sections read well
num = _num
parse_date = _parse_date


def read_grid(path: str) -> list:
    """Raw rows (list of lists) from a CSV/Excel/PDF — no interpretation. Lazy parsers."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".csv", ".txt", ".tsv"):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        return list(csv.reader(io.StringIO(text), dialect))
    if ext in (".xlsx", ".xls"):
        try:
            import openpyxl
        except ImportError:
            raise StatementError(_need(ext))
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        return [[("" if c is None else c) for c in row]
                for row in wb.active.iter_rows(values_only=True)]
    if ext == ".pdf":
        try:
            import pdfplumber
        except ImportError:
            raise StatementError(_need(".pdf"))
        rows = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables() or []:
                    rows.extend(table)
        return rows
    raise StatementError(f"unsupported file type '{ext}' — give me .csv, .xlsx, or .pdf")


def read_text(path: str) -> str:
    """Plain text from a .txt/.md/.csv directly, or extracted from a PDF (lazy pdfplumber)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        try:
            import pdfplumber
        except ImportError:
            raise StatementError(_need(".pdf"))
        out = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                out.append(page.extract_text() or "")
        return "\n".join(out)
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def col_index(header_row: list, names) -> int | None:
    """First column whose header matches one of `names` (exact, then word-substring)."""
    low = [str(c or "").strip().lower() for c in header_row]
    for j, cell in enumerate(low):
        if cell in names:
            return j
    for j, cell in enumerate(low):
        if any(n in cell for n in names if len(n) >= 4):
            return j
    return None
