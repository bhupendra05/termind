"""CA workbench v2.3–v2.6 — scrutiny, GST recon, notice drafting, Schedule III financials.

All four sections run locally on confidential client data: a ledger, a GST register, a notice,
a trial balance. Deterministic cores with the local model only for the leftovers.
"""
import pytest

from termind.ca import scrutiny, gst, notice, finstmt
from termind.ca.bank import Txn


# ── v2.3 ledger scrutiny ────────────────────────────────────────────────────────
def test_scrutiny_flags_personal_and_round():
    txns = [
        Txn("2025-04-10", "Diwali gift to family", debit=21000.0),     # personal (high)
        Txn("2025-04-11", "Provision for expenses", debit=50000.0),    # round (medium)
        Txn("2025-04-12", "Office supplies Staples", debit=1234.0),    # clean
    ]
    flags = scrutiny.scrutinize(txns)
    kinds = {f.kind for f in flags}
    assert "possible personal expense" in kinds
    assert any(f.severity == "high" for f in flags)
    assert "round amount" in kinds


def test_scrutiny_duplicates_and_weekend_and_missing():
    txns = [
        Txn("2025-04-01", "Consulting fee XYZ", debit=11800.0),
        Txn("2025-04-03", "Consulting fee XYZ", debit=11800.0),        # duplicate pair
        Txn("2025-01-04", "Saturday posting", debit=900.0),            # 2025-01-04 is a Saturday
        Txn("2025-04-05", "", debit=700.0),                            # missing narration
    ]
    flags = scrutiny.scrutinize(txns)
    kinds = {f.kind for f in flags}
    assert "possible duplicate" in kinds
    assert "weekend entry" in kinds
    assert "missing narration" in kinds


def test_scrutiny_outlier_spike():
    txns = [Txn(f"2025-04-{d:02d}", f"routine exp {d}", debit=400.0 + d * 10) for d in range(1, 13)]
    txns.append(Txn("2025-04-15", "Capital machinery purchase", debit=2_000_000.0))
    flags = scrutiny.scrutinize(txns)
    assert any(f.kind == "unusual spike" for f in flags)


def test_benford_distribution_shape():
    txns = [Txn("2025-04-01", "x", debit=float(v)) for v in
            [12, 15, 19, 23, 28, 34, 41, 55, 70, 95, 110, 150, 230, 480, 910]]
    bf = scrutiny.benford(txns)
    assert bf["n"] == 15
    assert abs(sum(bf["observed"].values()) - 1.0) < 0.001
    assert bf["observed"][1] > bf["observed"][9]                      # Benford: 1 leads


def test_scrutiny_summary_and_csv_and_narrative():
    txns = [Txn("2025-04-10", "Personal salon visit", debit=5000.0),
            Txn("2025-04-11", "Genuine vendor bill", debit=3333.0)]
    flags = scrutiny.scrutinize(txns)
    s = scrutiny.summary(flags, txns)
    assert s["transactions"] == 2 and s["flags"] >= 1 and s["high"] >= 1
    assert "Severity,Kind" in scrutiny.to_csv(flags).splitlines()[0]
    assert scrutiny.narrative(flags, s) and "flagged" in scrutiny.narrative(flags, s)


# ── v2.4 GST reconciliation ───────────────────────────────────────────────────────
BOOKS = """GSTIN of Supplier,Invoice No,Invoice Date,Taxable Value,IGST,CGST,SGST
27AAAAA0000A1Z5,INV-001,01-04-2025,100000,18000,0,0
27BBBBB1111B1Z5,INV-100,02-04-2025,50000,9000,0,0
27CCCCC2222C1Z5,PUR-9,03-04-2025,20000,0,1800,1800
"""
PORTAL = """GSTIN of Supplier,Invoice No,Invoice Date,Taxable Value,IGST,CGST,SGST
27AAAAA0000A1Z5,INV-001,01-04-2025,100000,18000,0,0
27BBBBB1111B1Z5,INV-1OO,02-04-2025,50000,9000,0,0
27DDDDD3333D1Z5,SUP-5,05-04-2025,30000,5400,0,0
"""


def test_gst_parse_sums_tax_components():
    inv = gst.parse_invoices(text=BOOKS, source="books")
    assert len(inv) == 3
    pur = next(i for i in inv if i.raw_no == "PUR-9")
    assert pur.tax == 3600.0 and pur.taxable == 20000.0               # cgst+sgst summed


def test_gst_reconcile_buckets():
    books = gst.parse_invoices(text=BOOKS, source="books")
    portal = gst.parse_invoices(text=PORTAL, source="portal")
    res = gst.reconcile(books, portal)
    assert len(res["matched"]) == 1                                  # INV-001 exact
    assert len(res["probable_invoice_typo"]) == 1                    # INV-100 vs INV-1OO
    assert len(res["in_books_not_2b"]) == 1                          # PUR-9 → ITC at risk
    assert len(res["in_2b_not_books"]) == 1                          # SUP-5 unbooked
    s = gst.summary(res)
    assert s["itc_at_risk"] == 3600.0
    assert "in books, not in 2B" in gst.to_csv(res)


def test_gst_value_mismatch_detected():
    b = gst.parse_invoices(text=(
        "GSTIN of Supplier,Invoice No,Taxable Value,IGST\n27AAAAA0000A1Z5,X1,100000,18000\n"),
        source="books")
    p = gst.parse_invoices(text=(
        "GSTIN of Supplier,Invoice No,Taxable Value,IGST\n27AAAAA0000A1Z5,X1,100000,17000\n"),
        source="portal")
    res = gst.reconcile(b, p)
    assert len(res["value_mismatch"]) == 1 and not res["matched"]


def test_gst_normalize_invoice():
    assert gst.normalize_invoice("INV/001") == "INV001"
    assert gst.normalize_invoice("0042") == "42"
    assert gst.normalize_invoice("inv-1oo") == "INV1OO"


# ── v2.5 notice-reply drafting ────────────────────────────────────────────────────
def test_notice_classify_gst_and_income_tax():
    assert notice.classify_notice("Notice in FORM GST ASMT-10 for the period…").section == "ASMT-10"
    n = notice.classify_notice("…hereby issued under section 143(2) of the Income-tax Act…")
    assert n.law == "Income-Tax" and n.section == "143(2)"
    itc = notice.classify_notice("Discrepancy: ITC availed in 3B exceeds 2B for the period")
    assert itc.law == "GST" and "ITC" in itc.kind


def test_notice_extracts_amounts():
    n = notice.classify_notice("A demand of ₹1,50,000 and interest of Rs. 12,500 is raised.")
    assert 150000.0 in n.amounts and 12500.0 in n.amounts


def test_notice_skeleton_offline_is_pointwise():
    out = notice.draft_reply("notice u/s 143(2): mismatch in income vs 26AS", facts="Income reconciles to 26AS")
    assert out["by_model"] is False
    assert "143(2)" in out["draft"] and "Enclosures" in out["draft"]
    assert "1." in out["draft"]                                       # point-wise


def test_notice_uses_local_model_when_present():
    out = notice.draft_reply("ASMT-10 ITC mismatch", brain=lambda m: "DRAFTED POINTWISE REPLY")
    assert out["by_model"] is True and out["draft"] == "DRAFTED POINTWISE REPLY"


# ── v2.6 Schedule III financials ──────────────────────────────────────────────────
TB = """Particulars,Debit,Credit
Sales,,500000
Purchases,300000,
Rent,60000,
Salaries,80000,
Sundry Debtors,150000,
HDFC Bank,130000,
Share Capital,,200000
Sundry Creditors,,20000
"""


def test_trial_balance_parse_and_map():
    bals = finstmt.parse_trial_balance(text=TB)
    assert len(bals) == 8
    mapped = finstmt.map_to_schedule3(bals)
    by_name = {m["ledger"].name: m for m in mapped}
    assert by_name["Sales"]["group"] == "Revenue from operations"
    assert by_name["Rent"]["group"] == "Other expenses"
    assert by_name["Sundry Debtors"]["group"] == "Trade receivables"
    assert by_name["HDFC Bank"]["group"] == "Cash & cash equivalents"
    assert by_name["Share Capital"]["group"] == "Share capital / Capital"


def test_statements_balance_and_profit():
    mapped = finstmt.map_to_schedule3(finstmt.parse_trial_balance(text=TB))
    st = finstmt.build_statements(mapped)
    assert st["pnl"]["profit_before_tax"] == 60000.0                  # 500000 - (300000+60000+80000)
    assert st["bs"]["balanced"] is True
    text = finstmt.to_text(st)
    assert "STATEMENT OF PROFIT & LOSS" in text and "BALANCE SHEET" in text and "✓ balanced" in text


def test_unmapped_ledger_falls_back_without_brain():
    mapped = finstmt.map_to_schedule3([finstmt.LedgerBalance("Zyzzyx Suspense", 5000.0, 0.0)])
    assert mapped[0]["statement"] == "BS" and mapped[0]["basis"] == "fallback"


# ── REPL dispatch + web endpoints for every section ───────────────────────────────
def test_ca_terminal_dispatch_all_sections(tmp_path):
    import termind.repl as r
    s = r.Session(live=False)
    s.store["workspace"] = str(tmp_path)
    (tmp_path / "led.csv").write_text(
        "Date,Narration,Withdrawal Amt\n2025-04-10,Diwali gift family,21000\n")
    (tmp_path / "books.csv").write_text(BOOKS)
    (tmp_path / "portal.csv").write_text(PORTAL)
    (tmp_path / "n.txt").write_text("Notice u/s 143(2): mismatch with 26AS. Demand ₹1,50,000.")
    (tmp_path / "tb.csv").write_text(TB)
    assert "scrutiny" in s.handle("/ca scrutiny led.csv").lower()
    assert "ITC at risk" in s.handle("/ca gst books.csv portal.csv")
    assert "143(2)" in s.handle("/ca notice n.txt")
    assert "Schedule III" in s.handle("/ca fs tb.csv")
    tools = {e["tool"] for e in s.ledger.entries}
    assert {"ca.scrutiny.report", "ca.gst.report", "ca.notice.draft", "ca.fs.report"} <= tools


def _post_ca(port, body, workspace):
    import json, threading, urllib.request
    import termind.repl as r
    from termind.web import serve
    s = r.Session(live=False)
    s.store["workspace"] = str(workspace)
    httpd, url = serve(s, port=port, open_browser=False)
    threading.Thread(target=httpd.handle_request, daemon=True).start()
    req = urllib.request.Request(url + "/api/ca", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    rep = json.loads(urllib.request.urlopen(req, timeout=8).read())
    httpd.server_close()
    return rep


def test_web_ca_scrutiny_endpoint(tmp_path):
    rep = _post_ca(8831, {"op": "scrutiny", "filename": "led.csv",
                          "content": "Date,Narration,Withdrawal Amt\n2025-04-10,Personal salon,9000\n"},
                   tmp_path)
    assert rep["ok"] and rep["summary"]["high"] >= 1 and "csv_content" in rep


def test_web_ca_gst_endpoint(tmp_path):
    rep = _post_ca(8833, {"op": "gst", "books_name": "b.csv", "books_content": BOOKS,
                          "portal_name": "p.csv", "portal_content": PORTAL}, tmp_path)
    assert rep["ok"] and rep["summary"]["itc_at_risk"] == 3600.0


def test_web_ca_notice_endpoint(tmp_path):
    rep = _post_ca(8835, {"op": "notice", "filename": "notice.txt",
                          "content": "Scrutiny notice under section 143(2) — mismatch with 26AS."},
                   tmp_path)
    assert rep["ok"] and rep["notice"]["section"] == "143(2)" and rep["md_content"]


def test_web_ca_fs_endpoint(tmp_path):
    rep = _post_ca(8837, {"op": "fs", "filename": "tb.csv", "content": TB}, tmp_path)
    assert rep["ok"] and rep["statements"]["bs"]["balanced"] is True


def test_ca_panel_has_all_sections():
    from termind.web import PAGE
    for marker in ("id=scrrun", "id=gstrun", "id=notrun", "id=fsrun", "Ledger scrutiny",
                   "GST 2B reconciliation", "Notice reply", "Schedule III",
                   "caGather('scrutiny'", "op:'gst'", "op:'notice'", "caGather('fs'"):
        assert marker in PAGE, marker
