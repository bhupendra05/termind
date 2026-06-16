"""Bank statement → ledger / Tally — CA workbench v2.2.

The single biggest monthly grind in an Indian CA practice: a client's bank statement comes in
as a PDF/Excel, and someone re-keys hundreds of lines into Tally, guessing a ledger head from
each cryptic narration ("NEFT DR-HDFC-RAZORPAY-…"). It eats hours per client and the bank data
is exactly the kind that must NOT be uploaded to a cloud tool.

This module does it locally and end-to-end:
  1. parse a statement (CSV is stdlib; XLSX/PDF lazy-load a parser into the workspace venv);
  2. classify every line → a ledger head + voucher type, deterministic rules first and the
     LOCAL model only for the leftovers (so most lines never need the model at all);
  3. export ready-to-import **Tally XML** vouchers + a ledger-ready CSV.

Pure-stdlib core. No network. The caller seals the parse/export into the audit ledger.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from xml.sax.saxutils import escape

# extension → the pip package that can read it (csv/txt need none — stdlib)
PARSERS = {".xlsx": "openpyxl", ".xls": "xlrd", ".pdf": "pdfplumber"}


class StatementError(Exception):
    """A statement could not be read (bad format, or a parser isn't installed yet)."""


@dataclass
class Txn:
    """One line of a bank statement, normalized. Money out is `debit`, money in is `credit`."""
    date: str               # ISO yyyy-mm-dd ("" if unparseable)
    narration: str
    debit: float = 0.0      # withdrawal / money leaving the account
    credit: float = 0.0     # deposit / money entering the account
    balance: float | None = None
    ref: str = ""

    @property
    def direction(self) -> str:
        return "out" if self.debit > 0 else "in"

    @property
    def amount(self) -> float:
        return self.debit if self.debit > 0 else self.credit


@dataclass
class Classified:
    """A transaction with its suggested posting. `basis` says how the head was chosen."""
    txn: Txn
    ledger: str             # the contra ledger head (the side that ISN'T the bank)
    voucher: str            # "Payment" | "Receipt" | "Contra"
    confidence: float       # 0..1
    basis: str              # "rule:<name>" | "llm" | "default"


# ── the classifier ──────────────────────────────────────────────────────────────
# Each rule: (name, regex, ledger-out, ledger-in, voucher). ledger-in lets a single
# keyword post differently by direction (interest paid vs interest earned). A `None`
# ledger on a side means "rule doesn't apply in that direction".
SUSPENSE = "Suspense (to be classified)"
_RULES = [
    ("salary",      r"\b(salary|sal\b|payroll|wages|stipend)\b",            "Salaries", None, "Payment"),
    ("gst",         r"\b(gst|cgst|sgst|igst|gstn|gst[\s-]*paid)\b",         "GST Paid", None, "Payment"),
    ("tds",         r"\b(tds|tcs|tax deduct|26q|24q)\b",                    "TDS Payable", None, "Payment"),
    ("rent",        r"\brent\b",                                            "Rent", "Rent Received", "Payment"),
    ("electricity", r"\b(electric\w*|msedcl|mseb|bescom|tata power|adani elec|torrent power)\b",
                    "Electricity Charges", None, "Payment"),
    ("telecom",     r"\b(airtel|jio|vodafone|vi\b|bsnl|broadband|internet|telephone|mobile recharge)\b",
                    "Telephone & Internet", None, "Payment"),
    ("fuel",        r"\b(fuel|petrol|diesel|hpcl|iocl|bpcl|indian oil|bharat petroleum)\b",
                    "Fuel & Conveyance", None, "Payment"),
    ("insurance",   r"\b(insurance|lic\b|premium|hdfc life|policy)\b",      "Insurance", None, "Payment"),
    ("interest",    r"\b(interest|int\.?\s*(pd|paid|cr|coll))\b",
                    "Interest Expense", "Interest Income", "Payment"),
    ("bankcharge",  r"\b(bank\s*charge|chrg|chg\b|amc|sms chg|min(imum)? bal|nwd|proc(essing)? fee|annual fee|imps chg|neft chg)\b",
                    "Bank Charges", None, "Payment"),
    ("loan",        r"\b(emi|loan|ecs|nach|repayment|installment|instalment)\b",
                    "Loan Repayment", "Loan Received", "Payment"),
    ("dividend",    r"\b(dividend|div\b)\b",                                None, "Dividend Income", "Receipt"),
    ("refund",      r"\b(refund|reversal|revrsl|returned|chargeback)\b",    None, "Refund Received", "Receipt"),
    ("purchase",    r"\b(amazon|flipkart|reliance retail|dmart|purchase|vendor|supplier)\b",
                    "Purchases", None, "Payment"),
    ("welfare",     r"\b(swiggy|zomato|restaurant|canteen|tea|coffee)\b",   "Staff Welfare", None, "Payment"),
    ("courier",     r"\b(dtdc|bluedart|delhivery|courier|postage|fedex)\b", "Postage & Courier", None, "Payment"),
    ("professional", r"\b(audit fee|consult\w*|professional|legal fee|retainer)\b",
                     "Professional Fees", "Professional Income", "Payment"),
]
# cash / self transfers are Contra, not Payment/Receipt — they move money between own books
_CONTRA = re.compile(r"\b(cash dep|cash wdl|cash withdrawal|atm|atw|self|to self|own a/?c|sweep)\b", re.I)
_COMPILED = [(n, re.compile(rx, re.I), lo, li, v) for (n, rx, lo, li, v) in _RULES]


def _classify_rule(t: Txn) -> Classified:
    """Deterministic first pass: keyword → ledger head. Returns basis='default' if no rule hits."""
    narr = t.narration or ""
    if _CONTRA.search(narr):
        return Classified(t, "Cash", "Contra", 0.85, "rule:contra")
    for name, rx, lo, li, voucher in _COMPILED:
        if rx.search(narr):
            ledger = lo if t.direction == "out" else (li or lo)
            if ledger is None:                       # rule doesn't apply this direction
                continue
            v = voucher if t.direction == "out" else ("Receipt" if voucher == "Payment" else voucher)
            return Classified(t, ledger, v, 0.9, f"rule:{name}")
    voucher = "Payment" if t.direction == "out" else "Receipt"
    return Classified(t, SUSPENSE, voucher, 0.0, "default")


def _classify_llm(txns: list, out: list, idx: list, brain) -> None:
    """Ask the LOCAL model to name a ledger head for the leftovers, in ONE batched JSON call."""
    items = [{"i": i, "narration": txns[i].narration[:140], "direction": txns[i].direction}
             for i in idx]
    prompt = (
        "You are an Indian accountant mapping bank narrations to Tally ledger heads. "
        "For each item, give the most likely standard ledger head (e.g. 'Rent', 'Bank Charges', "
        "'Sundry Creditors', 'Sales', 'Office Expenses'). direction 'out' = money paid, "
        "'in' = money received. Reply ONLY JSON: {\"map\":[{\"i\":<int>,\"ledger\":\"...\"}]}.\n\n"
        + str(items))
    try:
        raw = brain([{"role": "user", "content": prompt}])
        data = _loads(raw)
        by_i = {int(m["i"]): str(m["ledger"]).strip() for m in data.get("map", []) if m.get("ledger")}
    except Exception:
        return                                       # leave them in Suspense — never guess silently
    for i in idx:
        head = by_i.get(i)
        if head and head.lower() not in ("", "suspense", "unknown", "n/a"):
            out[i] = Classified(txns[i], head, out[i].voucher, 0.6, "llm")


def _loads(raw: str) -> dict:
    import json
    t = (raw or "").strip().strip("`")
    if t[:4].lower() == "json":
        t = t[4:]
    a, b = t.find("{"), t.rfind("}")
    return json.loads(t[a:b + 1]) if a >= 0 else {}


def classify(txns: list, brain=None) -> list:
    """Classify every transaction. Rules handle the obvious ones; `brain` (the local model,
    optional) names a head for whatever's left. Without a brain, leftovers stay in Suspense."""
    out = [_classify_rule(t) for t in txns]
    leftovers = [i for i, c in enumerate(out) if c.basis == "default"]
    if brain and leftovers:
        _classify_llm(txns, out, leftovers, brain)
    return out


# ── parsing ──────────────────────────────────────────────────────────────────────
_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y",
                 "%d-%b-%Y", "%d-%b-%y", "%d %b %Y", "%m/%d/%Y", "%d.%m.%Y")
_COLS = {
    "date": ("date", "txn date", "value date", "transaction date", "posting date", "tran date"),
    "narration": ("narration", "description", "particulars", "details", "remarks",
                  "transaction", "naration", "transaction remarks"),
    "debit": ("debit", "withdrawal", "withdrawal amt", "withdrawal amt.", "dr", "paid out",
              "withdrawals", "debit amount", "amount debited"),
    "credit": ("credit", "deposit", "deposit amt", "deposit amt.", "cr", "paid in",
               "deposits", "credit amount", "amount credited"),
    "balance": ("balance", "closing balance", "running balance", "available balance"),
    "ref": ("ref", "ref no", "reference", "cheque", "chq", "chq no", "cheque no", "ref no."),
}


def _parse_date(s: str) -> str:
    s = (s or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _num(s) -> float:
    if s is None:
        return 0.0
    s = str(s).strip().replace(",", "").replace("₹", "").replace("Rs.", "").replace("Rs", "")
    if not s or s in ("-", "—"):
        return 0.0
    neg = s.startswith("(") and s.endswith(")")        # (1,200) = -1200 in some exports
    s = s.strip("()").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return 0.0
    v = float(m.group(0))
    return -v if neg else v


def _match_header(cells: list) -> dict | None:
    """Map a header row → {field: column index}. Needs at least date + narration to count."""
    low = [str(c or "").strip().lower() for c in cells]
    found = {}
    for field_name, names in _COLS.items():
        for j, cell in enumerate(low):
            if cell in names or any(cell == n for n in names):
                found[field_name] = j
                break
    if "date" not in found:                            # looser fallback: header contains the word
        for j, cell in enumerate(low):
            if "date" in cell:
                found["date"] = j
                break
    if "narration" not in found:
        for j, cell in enumerate(low):
            if any(k in cell for k in ("narrat", "descrip", "particular", "remark", "detail")):
                found["narration"] = j
                break
    return found if "date" in found and "narration" in found else None


def parse_rows(rows: list) -> list:
    """Turn a grid of cells (any source) into normalized Txns. Finds the header row itself."""
    header, start = None, 0
    for i, r in enumerate(rows[:25]):                  # the header is near the top
        header = _match_header(r)
        if header:
            start = i + 1
            break
    if not header:
        raise StatementError(
            "couldn't find a Date + Narration header — check the file, or pass an explicit mapping")
    txns = []
    di, ci = header.get("debit"), header.get("credit")
    for r in rows[start:]:
        if not any(str(c).strip() for c in r):
            continue
        date = _parse_date(r[header["date"]]) if header["date"] < len(r) else ""
        narr = str(r[header["narration"]]).strip() if header["narration"] < len(r) else ""
        if not date and not narr:
            continue
        debit = _num(r[di]) if di is not None and di < len(r) else 0.0
        credit = _num(r[ci]) if ci is not None and ci < len(r) else 0.0
        # single amount column (sign carries direction) if there's no split debit/credit
        if di is None and ci is None:
            continue
        bal = _num(r[header["balance"]]) if "balance" in header and header["balance"] < len(r) else None
        ref = str(r[header["ref"]]).strip() if "ref" in header and header["ref"] < len(r) else ""
        if debit == 0.0 and credit == 0.0:
            continue                                   # opening-balance / summary lines
        txns.append(Txn(date, narr, abs(debit), abs(credit), bal, ref))
    return txns


def parse_csv(text: str) -> list:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    return parse_rows(rows)


def parse_statement(path: str) -> list:
    """Parse a statement file by extension. CSV/TSV is stdlib; XLSX/XLS/PDF lazy-load a parser
    into the workspace venv and, if it's missing, raise a clean 'install this' message."""
    import os
    ext = os.path.splitext(path)[1].lower()
    if ext in (".csv", ".txt", ".tsv"):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return parse_csv(f.read())
    if ext in (".xlsx", ".xls"):
        return _parse_xlsx(path, ext)
    if ext == ".pdf":
        return _parse_pdf(path)
    raise StatementError(f"unsupported statement type '{ext}' — give me .csv, .xlsx, or .pdf")


def _need(ext: str) -> str:
    pkg = PARSERS.get(ext, "?")
    return (f"reading {ext} needs '{pkg}', which isn't in the workspace venv yet. "
            f"install it there:  .venv/bin/pip install {pkg}   (or export to CSV — that's zero-setup)")


def _parse_xlsx(path: str, ext: str) -> list:
    try:
        import openpyxl
    except ImportError:
        raise StatementError(_need(ext))
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = [[("" if c is None else c) for c in row] for row in wb.active.iter_rows(values_only=True)]
    return parse_rows(rows)


def _parse_pdf(path: str) -> list:
    try:
        import pdfplumber
    except ImportError:
        raise StatementError(_need(".pdf"))
    rows = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                rows.extend(table)
    if not rows:
        raise StatementError("no tables found in the PDF — it may be scanned (image). "
                             "export the statement as Excel/CSV from net-banking instead")
    return parse_rows(rows)


# ── export: Tally XML + ledger CSV ────────────────────────────────────────────────
def _tally_date(iso: str) -> str:
    return iso.replace("-", "") if iso else ""


def _ledger_line(name: str, deemed_positive: bool, amount: float) -> str:
    sign = -amount if deemed_positive else amount      # Tally: debit is negative & "deemed positive"
    return ("    <ALLLEDGERENTRIES.LIST>\n"
            f"     <LEDGERNAME>{escape(name)}</LEDGERNAME>\n"
            f"     <ISDEEMEDPOSITIVE>{'Yes' if deemed_positive else 'No'}</ISDEEMEDPOSITIVE>\n"
            f"     <AMOUNT>{sign:.2f}</AMOUNT>\n"
            "    </ALLLEDGERENTRIES.LIST>\n")


def _voucher(c: Classified, bank_ledger: str) -> str:
    t = c.txn
    amt = t.amount
    # which side is debited depends on the voucher type
    if c.voucher == "Receipt":                         # money in: Dr Bank, Cr income/party
        dr, cr = bank_ledger, c.ledger
    elif c.voucher == "Contra":                        # bank out → Dr Cash, Cr Bank (and vice-versa)
        dr, cr = (c.ledger, bank_ledger) if t.direction == "out" else (bank_ledger, c.ledger)
    else:                                              # Payment, money out: Dr expense, Cr Bank
        dr, cr = c.ledger, bank_ledger
    body = (f'   <VOUCHER VCHTYPE="{c.voucher}" ACTION="Create" OBJVIEW="Accounting Voucher View">\n'
            f"    <DATE>{_tally_date(t.date)}</DATE>\n"
            f"    <NARRATION>{escape(t.narration)}</NARRATION>\n"
            f"    <VOUCHERTYPENAME>{c.voucher}</VOUCHERTYPENAME>\n"
            + _ledger_line(dr, True, amt)
            + _ledger_line(cr, False, amt)
            + "   </VOUCHER>\n")
    return body


def to_tally_xml(classified: list, company: str = "", bank_ledger: str = "Bank") -> str:
    """A Tally 'Import Data' envelope of accounting vouchers, ready to import via Gateway →
    Import → Vouchers. Bank/Cash is the constant contra; each line's suggested head is the other."""
    msgs = "".join('   <TALLYMESSAGE xmlns:UDF="TallyUDF">\n' + _voucher(c, bank_ledger)
                   + "   </TALLYMESSAGE>\n" for c in classified)
    return ("<ENVELOPE>\n"
            " <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>\n"
            " <BODY>\n"
            "  <IMPORTDATA>\n"
            "   <REQUESTDESC>\n"
            "    <REPORTNAME>Vouchers</REPORTNAME>\n"
            f"    <STATICVARIABLES><SVCURRENTCOMPANY>{escape(company)}</SVCURRENTCOMPANY>"
            "</STATICVARIABLES>\n"
            "   </REQUESTDESC>\n"
            "   <REQUESTDATA>\n"
            + msgs +
            "   </REQUESTDATA>\n"
            "  </IMPORTDATA>\n"
            " </BODY>\n"
            "</ENVELOPE>\n")


def to_csv(classified: list) -> str:
    """A flat, human-checkable ledger CSV — the CA reviews/edits this before importing."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Narration", "Debit", "Credit", "Voucher", "Ledger", "Confidence", "Basis"])
    for c in classified:
        t = c.txn
        w.writerow([t.date, t.narration, f"{t.debit:.2f}" if t.debit else "",
                    f"{t.credit:.2f}" if t.credit else "", c.voucher, c.ledger,
                    f"{c.confidence:.2f}", c.basis])
    return buf.getvalue()


def summary(classified: list) -> dict:
    """Totals + a per-ledger breakdown + how many lines still need a human eye."""
    by_ledger: dict = {}
    total_in = total_out = 0.0
    for c in classified:
        t = c.txn
        total_in += t.credit
        total_out += t.debit
        slot = by_ledger.setdefault(c.ledger, {"count": 0, "amount": 0.0})
        slot["count"] += 1
        slot["amount"] += t.amount
    review = [c for c in classified if c.ledger == SUSPENSE or c.confidence < 0.5]
    return {
        "transactions": len(classified),
        "total_in": round(total_in, 2),
        "total_out": round(total_out, 2),
        "ledgers": {k: {"count": v["count"], "amount": round(v["amount"], 2)}
                    for k, v in sorted(by_ledger.items(), key=lambda kv: -kv[1]["amount"])},
        "auto_classified": sum(1 for c in classified if c.basis.startswith("rule")),
        "by_model": sum(1 for c in classified if c.basis == "llm"),
        "needs_review": len(review),
    }
