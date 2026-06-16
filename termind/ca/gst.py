"""GST reconciliation — CA workbench v2.4.

The compliance grind that triggers the most notices: matching the purchase register (the books)
against GSTR-2B (what suppliers actually filed). Mismatches mean either ITC you can't claim yet,
or ITC you claimed that a supplier hasn't reported — the exact thing the department issues notices
about. ICAI even ships an offline matcher (EasyRecon) because this data can't go to a cloud tool;
this does the match AND explains each bucket, locally.

Pure stdlib. Matches on (supplier GSTIN + normalized invoice no), with a value tolerance, then a
second pass that catches invoice-number typos by GSTIN + amount. Reuses tables.py for I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import tables


@dataclass
class Invoice:
    gstin: str
    invoice_no: str          # normalized (uppercase, alphanumeric)
    raw_no: str              # as it appeared in the file
    date: str
    taxable: float
    tax: float
    source: str              # "books" | "portal"

    @property
    def total(self) -> float:
        return round(self.taxable + self.tax, 2)


_ALIASES = {
    "gstin": ("gstin", "gstin of supplier", "supplier gstin", "gstin/uin", "gstin of supplier "),
    "invoice": ("invoice no", "invoice number", "inv no", "document number", "bill no",
                "invoice no.", "supplier invoice no"),
    "date": ("invoice date", "date", "document date", "bill date", "inv date"),
    "taxable": ("taxable value", "taxable amount", "taxable", "assessable value"),
    "igst": ("integrated tax", "igst", "integrated tax amount", "igst amount"),
    "cgst": ("central tax", "cgst", "central tax amount", "cgst amount"),
    "sgst": ("state tax", "sgst", "state/ut tax", "state tax amount", "sgst amount", "utgst"),
    "tax": ("tax amount", "total tax", "gst", "total tax amount"),
    "total": ("invoice value", "total", "total value", "total amount"),
}


def normalize_invoice(no: str) -> str:
    n = re.sub(r"[^A-Z0-9]", "", str(no or "").upper())
    stripped = n.lstrip("0")
    return stripped or n


def parse_invoices(path: str = None, text: str = None, source: str = "books") -> list:
    """Parse a GSTR-2B export or purchase register into Invoices. Sums IGST+CGST+SGST for tax."""
    if text is not None:
        import csv
        import io
        rows = list(csv.reader(io.StringIO(text)))
    else:
        rows = tables.read_grid(path)
    header, start = None, 0
    for i, r in enumerate(rows[:30]):
        gi = tables.col_index(r, _ALIASES["gstin"])
        ii = tables.col_index(r, _ALIASES["invoice"])
        if gi is not None and ii is not None:
            header, start = r, i + 1
            break
    if header is None:
        raise tables.StatementError(
            "couldn't find GSTIN + Invoice columns — is this a GSTR-2B / purchase register export?")
    idx = {k: tables.col_index(header, v) for k, v in _ALIASES.items()}
    out = []
    for r in rows[start:]:
        if not any(str(c).strip() for c in r):
            continue
        def cell(key):
            j = idx.get(key)
            return r[j] if j is not None and j < len(r) else ""
        gstin = str(cell("gstin")).strip().upper()
        raw_no = str(cell("invoice")).strip()
        if not raw_no and not gstin:
            continue
        tax = (tables.num(cell("igst")) + tables.num(cell("cgst")) + tables.num(cell("sgst")))
        if tax == 0:
            tax = tables.num(cell("tax"))
        taxable = tables.num(cell("taxable"))
        if taxable == 0 and tax == 0 and tables.num(cell("total")) == 0:
            continue
        out.append(Invoice(gstin, normalize_invoice(raw_no), raw_no,
                           tables.parse_date(str(cell("date"))), abs(taxable), abs(tax), source))
    return out


def reconcile(books: list, portal: list, value_tol: float = 1.0) -> dict:
    """Match books (purchase register) against portal (GSTR-2B). Returns buckets of Invoices."""
    index = {}
    for inv in portal:
        index.setdefault((inv.gstin, inv.invoice_no), []).append(inv)
    matched, value_mismatch, in_books_not_2b = [], [], []
    used = set()
    for b in books:
        cands = index.get((b.gstin, b.invoice_no), [])
        hit = next((p for p in cands if id(p) not in used), None)
        if hit is not None:
            used.add(id(hit))
            if abs(hit.taxable - b.taxable) <= value_tol and abs(hit.tax - b.tax) <= value_tol:
                matched.append({"books": b, "portal": hit})
            else:
                value_mismatch.append({"books": b, "portal": hit})
        else:
            in_books_not_2b.append(b)
    # second pass: catch invoice-number typos by (GSTIN + taxable value)
    leftover_portal = [p for p in portal if id(p) not in used]
    pidx = {}
    for p in leftover_portal:
        pidx.setdefault((p.gstin, round(p.taxable)), p)
    probable_typos, still_missing = [], []
    for b in in_books_not_2b:
        p = pidx.get((b.gstin, round(b.taxable)))
        if p is not None and id(p) not in used:
            used.add(id(p))
            probable_typos.append({"books": b, "portal": p})
        else:
            still_missing.append(b)
    in_2b_not_books = [p for p in portal if id(p) not in used]
    return {
        "matched": matched,
        "value_mismatch": value_mismatch,
        "probable_invoice_typo": probable_typos,
        "in_books_not_2b": still_missing,     # ITC claimed but supplier hasn't filed → at risk
        "in_2b_not_books": in_2b_not_books,   # ITC available but not booked
    }


def summary(result: dict) -> dict:
    risk = sum(b.tax for b in result["in_books_not_2b"])
    available = sum(p.tax for p in result["in_2b_not_books"])
    return {
        "matched": len(result["matched"]),
        "value_mismatch": len(result["value_mismatch"]),
        "probable_invoice_typo": len(result["probable_invoice_typo"]),
        "in_books_not_2b": len(result["in_books_not_2b"]),
        "in_2b_not_books": len(result["in_2b_not_books"]),
        "itc_at_risk": round(risk, 2),          # tax you claimed that 2B doesn't support
        "itc_available_unbooked": round(available, 2),
    }


def to_csv(result: dict) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Bucket", "GSTIN", "Invoice (books)", "Invoice (2B)", "Taxable", "Tax", "Note"])
    for m in result["matched"]:
        b = m["books"]
        w.writerow(["matched", b.gstin, b.raw_no, m["portal"].raw_no, f"{b.taxable:.2f}",
                    f"{b.tax:.2f}", "ok"])
    for m in result["value_mismatch"]:
        b, p = m["books"], m["portal"]
        w.writerow(["value mismatch", b.gstin, b.raw_no, p.raw_no, f"{b.taxable:.2f}",
                    f"{b.tax:.2f}", f"2B taxable {p.taxable:.2f} / tax {p.tax:.2f}"])
    for m in result["probable_invoice_typo"]:
        b, p = m["books"], m["portal"]
        w.writerow(["invoice-no typo?", b.gstin, b.raw_no, p.raw_no, f"{b.taxable:.2f}",
                    f"{b.tax:.2f}", "same GSTIN + value, different invoice no"])
    for b in result["in_books_not_2b"]:
        w.writerow(["in books, not in 2B", b.gstin, b.raw_no, "", f"{b.taxable:.2f}",
                    f"{b.tax:.2f}", "ITC AT RISK — supplier hasn't filed"])
    for p in result["in_2b_not_books"]:
        w.writerow(["in 2B, not in books", p.gstin, "", p.raw_no, f"{p.taxable:.2f}",
                    f"{p.tax:.2f}", "ITC available — not booked"])
    return buf.getvalue()
