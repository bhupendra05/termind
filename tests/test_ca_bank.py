"""CA workbench v2.2 — bank statement → ledger / Tally.

Covers the whole local pipeline: parse a messy Indian bank CSV, classify each line with
deterministic rules (and the local model only for leftovers), and emit valid Tally voucher XML
with the right debit/credit signs. No network, no cloud — the whole point of the CA tab.
"""
from xml.dom import minidom

import pytest

from termind.ca import bank
from termind.ca.bank import Txn, Classified, StatementError

CSV = """Date,Narration,Withdrawal Amt,Deposit Amt,Closing Balance
01/04/2025,NEFT DR-SALARY APRIL-EMP,50000.00,,1200000.00
02/04/2025,UPI-OFFICE RENT-LANDLORD,25000.00,,1175000.00
03/04/2025,GST PAYMENT CGST,18000.00,,1157000.00
04/04/2025,NEFT CR-ACME CORP INVOICE 42,,150000.00,1307000.00
05/04/2025,ATM CASH WDL,10000.00,,1297000.00
06/04/2025,BANK CHARGES SMS CHG,29.50,,1296970.50
07/04/2025,INT.PD INTEREST ON SB,,2500.00,1299470.50
08/04/2025,NEFT DR-XYZ TRANSFER ONLINE,7000.00,,1292470.50
"""


def _ledger_entries(xml: str):
    """[(ledgername, is_deemed_positive, amount)] for the first voucher in the envelope."""
    dom = minidom.parseString(xml)
    v = dom.getElementsByTagName("VOUCHER")[0]
    out = []
    for le in v.getElementsByTagName("ALLLEDGERENTRIES.LIST"):
        name = le.getElementsByTagName("LEDGERNAME")[0].firstChild.data
        dp = le.getElementsByTagName("ISDEEMEDPOSITIVE")[0].firstChild.data
        amt = float(le.getElementsByTagName("AMOUNT")[0].firstChild.data)
        out.append((name, dp, amt))
    return out


# ── parsing ───────────────────────────────────────────────────────────────────────
def test_parse_csv_splits_debit_credit_and_dates():
    txns = bank.parse_csv(CSV)
    assert len(txns) == 8
    assert txns[0].date == "2025-04-01"               # dd/mm/yyyy → ISO
    assert txns[0].debit == 50000.0 and txns[0].credit == 0.0
    assert txns[0].direction == "out"
    assert txns[3].credit == 150000.0 and txns[3].direction == "in"
    assert txns[0].balance == 1200000.0


def test_parse_csv_ignores_blank_and_amountless_lines():
    txns = bank.parse_csv("Date,Narration,Debit,Credit\n,,,\n10-04-2025,Real line,100,\n")
    assert len(txns) == 1 and txns[0].narration == "Real line"


def test_parse_rows_without_header_raises():
    with pytest.raises(StatementError):
        bank.parse_rows([["just", "some", "numbers"], ["1", "2", "3"]])


def test_parse_statement_rejects_unknown_extension(tmp_path):
    p = tmp_path / "stmt.docx"
    p.write_text("x")
    with pytest.raises(StatementError):
        bank.parse_statement(str(p))


def test_missing_parser_message_names_the_package():
    msg = bank._need(".xlsx")
    assert "openpyxl" in msg and "pip install" in msg and "CSV" in msg


def test_parse_statement_reads_csv_file(tmp_path):
    p = tmp_path / "stmt.csv"
    p.write_text(CSV)
    assert len(bank.parse_statement(str(p))) == 8


# ── classification ──────────────────────────────────────────────────────────────
def test_rules_map_common_narrations():
    cl = bank.classify(bank.parse_csv(CSV))             # no brain — rules only
    by_narr = {c.txn.narration: c for c in cl}
    assert by_narr["NEFT DR-SALARY APRIL-EMP"].ledger == "Salaries"
    assert by_narr["NEFT DR-SALARY APRIL-EMP"].voucher == "Payment"
    assert by_narr["UPI-OFFICE RENT-LANDLORD"].ledger == "Rent"
    assert by_narr["GST PAYMENT CGST"].ledger == "GST Paid"
    assert by_narr["BANK CHARGES SMS CHG"].ledger == "Bank Charges"


def test_contra_for_cash_and_atm():
    c = bank.classify([Txn("2025-04-05", "ATM CASH WDL", debit=10000.0)])[0]
    assert c.voucher == "Contra" and c.ledger == "Cash"


def test_interest_posts_by_direction():
    paid = bank.classify([Txn("2025-04-01", "INTEREST ON LOAN", debit=900.0)])[0]
    earned = bank.classify([Txn("2025-04-07", "INT.PD INTEREST ON SB", credit=2500.0)])[0]
    assert paid.ledger == "Interest Expense" and paid.voucher == "Payment"
    assert earned.ledger == "Interest Income" and earned.voucher == "Receipt"


def test_unknown_falls_to_suspense_without_a_brain():
    cl = bank.classify(bank.parse_csv(CSV))
    sus = [c for c in cl if c.ledger == bank.SUSPENSE]
    narrs = {c.txn.narration for c in sus}
    assert "NEFT CR-ACME CORP INVOICE 42" in narrs       # genuine unknowns stay in Suspense
    assert "NEFT DR-XYZ TRANSFER ONLINE" in narrs
    assert all(c.basis == "default" for c in sus)


def test_local_model_classifies_only_the_leftovers():
    calls = []

    def brain(messages):
        calls.append(messages)
        # the model should be asked ONLY about the two unknowns
        return '{"map":[{"i":3,"ledger":"Sales"},{"i":7,"ledger":"Sundry Creditors"}]}'

    cl = bank.classify(bank.parse_csv(CSV), brain=brain)
    by_narr = {c.txn.narration: c for c in cl}
    assert len(calls) == 1                               # one batched call, not one-per-line
    assert by_narr["NEFT CR-ACME CORP INVOICE 42"].ledger == "Sales"
    assert by_narr["NEFT CR-ACME CORP INVOICE 42"].basis == "llm"
    assert by_narr["NEFT DR-XYZ TRANSFER ONLINE"].ledger == "Sundry Creditors"
    # a rule-classified line is untouched by the model
    assert by_narr["UPI-OFFICE RENT-LANDLORD"].basis == "rule:rent"


def test_brain_failure_leaves_suspense_intact():
    def brain(_m):
        raise RuntimeError("model down")

    cl = bank.classify(bank.parse_csv(CSV), brain=brain)
    assert any(c.ledger == bank.SUSPENSE for c in cl)    # never crashes, never fabricates


# ── Tally XML export ──────────────────────────────────────────────────────────────
def test_tally_xml_is_wellformed_and_has_a_voucher_per_line():
    cl = bank.classify(bank.parse_csv(CSV))
    xml = bank.to_tally_xml(cl, company="Acme Pvt Ltd", bank_ledger="HDFC Bank")
    dom = minidom.parseString(xml)                       # raises if not well-formed
    assert len(dom.getElementsByTagName("VOUCHER")) == 8
    assert dom.getElementsByTagName("REPORTNAME")[0].firstChild.data == "Vouchers"


def test_payment_voucher_signs_are_correct():
    cl = bank.classify([Txn("2025-04-02", "OFFICE RENT", debit=25000.0)])
    entries = _ledger_entries(bank.to_tally_xml(cl, bank_ledger="HDFC Bank"))
    # Payment: Dr Rent (deemed-positive Yes, negative amount), Cr Bank (No, positive)
    assert ("Rent", "Yes", -25000.0) in entries
    assert ("HDFC Bank", "No", 25000.0) in entries


def test_receipt_voucher_signs_are_correct():
    cl = bank.classify([Txn("2025-04-04", "DIVIDEND CREDIT", credit=5000.0)])
    entries = _ledger_entries(bank.to_tally_xml(cl, bank_ledger="HDFC Bank"))
    # Receipt: Dr Bank (Yes, negative), Cr income (No, positive)
    assert ("HDFC Bank", "Yes", -5000.0) in entries
    assert ("Dividend Income", "No", 5000.0) in entries


def test_xml_escapes_special_characters():
    cl = bank.classify([Txn("2025-04-01", "PAY M/S A & B <CO>", debit=100.0)])
    xml = bank.to_tally_xml(cl, company="R&D <Labs>")
    minidom.parseString(xml)                             # would raise on raw & or <
    assert "&amp;" in xml and "&lt;" in xml


# ── CSV export + summary ──────────────────────────────────────────────────────────
def test_ledger_csv_has_review_columns():
    cl = bank.classify(bank.parse_csv(CSV))
    out = bank.to_csv(cl)
    header = out.splitlines()[0]
    for col in ("Date", "Narration", "Voucher", "Ledger", "Confidence", "Basis"):
        assert col in header


def test_summary_totals_and_review_count():
    cl = bank.classify(bank.parse_csv(CSV))
    s = bank.summary(cl)
    assert s["transactions"] == 8
    assert s["total_out"] == round(50000 + 25000 + 18000 + 10000 + 29.5 + 7000, 2)
    assert s["total_in"] == round(150000 + 2500, 2)
    assert s["needs_review"] >= 2                        # the two unknowns at least
    assert s["auto_classified"] >= 5


# ── REPL + web integration ────────────────────────────────────────────────────────
def test_ca_command_writes_outputs_and_audits(tmp_path):
    import termind.repl as r
    s = r.Session(live=False)                            # offline → rules only, no model needed
    s.store["workspace"] = str(tmp_path)
    (tmp_path / "stmt.csv").write_text(CSV)
    out = s.handle("/ca bank stmt.csv")
    assert "8 transactions" in out and "Tally import XML" in out
    assert (tmp_path / "stmt_tally.xml").exists()
    assert (tmp_path / "stmt_ledger.csv").exists()
    tools = [e["tool"] for e in s.ledger.tail(5)]        # both steps sealed into the ledger
    assert "ca.bank.parse" in tools and "ca.bank.export" in tools


def test_ca_help_lists_sections_and_handle_routes():
    from termind.repl import Session, CA_HELP, FEATURES
    assert "/ca bank" in FEATURES and "data never left" in CA_HELP
    assert "scrutiny" in CA_HELP and "gst" in CA_HELP and "notice" in CA_HELP    # all listed
    s = Session(live=False)
    assert "usage" in s.handle("/ca gst").lower()        # built now → prompts for two files
    assert "unknown CA section" in s.handle("/ca zzz")   # honest on a bad section


def test_web_ca_bank_endpoint(tmp_path):
    import json, threading, urllib.request
    import termind.repl as r
    from termind.web import serve
    s = r.Session(live=False)
    s.store["workspace"] = str(tmp_path)
    httpd, url = serve(s, port=8821, open_browser=False)
    threading.Thread(target=httpd.handle_request, daemon=True).start()
    body = {"op": "bank", "filename": "hdfc apr.csv", "content": CSV}
    req = urllib.request.Request(url + "/api/ca", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    rep = json.loads(urllib.request.urlopen(req, timeout=8).read())
    httpd.server_close()
    assert rep["ok"] is True
    assert rep["summary"]["transactions"] == 8
    assert rep["xml"].endswith("_tally.xml") and "<VOUCHER" in rep["xml_content"]
    assert (tmp_path / "hdfc_apr.csv").exists()          # upload landed in the workspace
    assert (tmp_path / "hdfc_apr_tally.xml").exists()    # filename sanitized + outputs written


def test_ca_panel_served():
    from termind.web import PAGE
    for marker in ("data-s=ca", "CA workbench", "id=carun", "caRender", "/api/ca",
                   "convert to Tally"):
        assert marker in PAGE, marker
