"""Financial statements / Schedule III — CA workbench v2.6.

Year-end: a trial balance becomes a Balance Sheet and Statement of Profit & Loss grouped per
Schedule III of the Companies Act. The mapping (this ledger → that line item) is mechanical and
repetitive, and the trial balance is confidential client data. So: deterministic keyword mapping
first, the LOCAL model only for ledgers the rules don't recognise, then aggregate into the two
statements with totals — all on the machine.

Pure stdlib. Reuses tables.py for input. Output is a clean text statement + a structured dict.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import tables


@dataclass
class LedgerBalance:
    name: str
    debit: float = 0.0
    credit: float = 0.0

    @property
    def net(self) -> float:
        return round(self.debit - self.credit, 2)   # +ve = debit (asset/expense)


# (regex, statement, group). statement: "BS" | "PL". First match wins.
_MAP = [
    # ── P&L ───────────────────────────────────────────────────────────────────
    (r"\b(sales|revenue|turnover|income from op|service income|professional income|fees? earned)\b",
     "PL", "Revenue from operations"),
    (r"\b(interest income|dividend|other income|discount received|commission received|rent received|profit on)\b",
     "PL", "Other income"),
    (r"\b(purchases?|raw materials?|material consumed|cost of goods)\b", "PL", "Purchases of stock-in-trade"),
    (r"\b(opening stock|closing stock|changes in inventor)\b", "PL", "Changes in inventories"),
    (r"\b(salary|salaries|wages|bonus|staff|employee|pf|esi|gratuity|director.?s? remuneration)\b",
     "PL", "Employee benefit expense"),
    (r"\b(interest (on|paid|expense)|finance cost|bank charge|loan processing)\b", "PL", "Finance costs"),
    (r"\b(depreciation|amortis|amortiz)\b", "PL", "Depreciation & amortisation"),
    (r"\b(rent|electricity|power|telephone|internet|repairs?|insurance|audit fee|legal|professional|"
     r"printing|stationery|travel|conveyance|postage|courier|office|misc|sundry expense|fuel|freight|"
     r"advertis|commission paid|donation|rates? and taxes|software|subscription)\b",
     "PL", "Other expenses"),
    # ── Balance Sheet: Equity & Liabilities ───────────────────────────────────
    (r"\b(share capital|equity|capital account|partner.?s? capital|proprietor)\b", "BS", "Share capital / Capital"),
    (r"\b(reserve|surplus|retained earning|general reserve|securities premium|p&l account)\b",
     "BS", "Reserves & surplus"),
    (r"\b(long[-\s]?term borrow|term loan|debenture|secured loan|unsecured loan|loan from)\b",
     "BS", "Long-term borrowings"),
    (r"\b(deferred tax liab)\b", "BS", "Deferred tax liabilities"),
    (r"\b(cash credit|bank overdraft|working capital loan|short[-\s]?term borrow)\b", "BS", "Short-term borrowings"),
    (r"\b(sundry creditor|trade payable|creditors|payable to|bills payable)\b", "BS", "Trade payables"),
    (r"\b(duties? and taxes|gst payable|tds payable|tcs payable|outstanding|expenses payable|"
     r"statutory|advance from customer|other liab)\b", "BS", "Other current liabilities"),
    (r"\b(provision for tax|provision for|short[-\s]?term provision)\b", "BS", "Short-term provisions"),
    # ── Balance Sheet: Assets ─────────────────────────────────────────────────
    (r"\b(fixed asset|plant|machinery|building|premises|furniture|fixture|vehicle|car\b|computer|"
     r"land|equipment|goodwill|intangible|office equipment)\b", "BS", "Property, plant & equipment"),
    (r"\b(investment|shares in|mutual fund)\b", "BS", "Non-current investments"),
    (r"\b(inventor|stock[-\s]?in[-\s]?trade|closing stock|raw material stock|finished goods)\b",
     "BS", "Inventories"),
    (r"\b(sundry debtor|trade receivable|debtors|receivable from|bills receivable)\b", "BS", "Trade receivables"),
    (r"\b(cash in hand|cash at bank|\bbank\b|cash\b|fixed deposit|\bfdr?\b|petty cash)\b",
     "BS", "Cash & cash equivalents"),
    (r"\b(loans? and advances?|advance to|deposit|prepaid|tds receivable|gst (input|receivable)|"
     r"input tax credit|security deposit)\b", "BS", "Short-term loans & advances"),
]
_COMPILED = [(re.compile(rx, re.I), st, grp) for (rx, st, grp) in _MAP]

# Schedule III presentation order
_BS_LIABS = ["Share capital / Capital", "Reserves & surplus", "Long-term borrowings",
             "Deferred tax liabilities", "Short-term borrowings", "Trade payables",
             "Other current liabilities", "Short-term provisions"]
_BS_ASSETS = ["Property, plant & equipment", "Non-current investments", "Inventories",
              "Trade receivables", "Cash & cash equivalents", "Short-term loans & advances"]
_PL_INCOME = ["Revenue from operations", "Other income"]
_PL_EXPENSE = ["Purchases of stock-in-trade", "Changes in inventories", "Employee benefit expense",
               "Finance costs", "Depreciation & amortisation", "Other expenses"]


def parse_trial_balance(path: str = None, text: str = None) -> list:
    """Parse a trial balance (Particulars/Ledger, Debit, Credit) into LedgerBalances."""
    if text is not None:
        import csv
        import io
        rows = list(csv.reader(io.StringIO(text)))
    else:
        rows = tables.read_grid(path)
    name_a = ("particulars", "ledger", "ledger name", "account", "name", "head", "particular")
    debit_a = ("debit", "dr", "debit amount", "debit balance")
    credit_a = ("credit", "cr", "credit amount", "credit balance")
    header, start = None, 0
    for i, r in enumerate(rows[:30]):
        ni = tables.col_index(r, name_a)
        di = tables.col_index(r, debit_a)
        ci = tables.col_index(r, credit_a)
        if ni is not None and (di is not None or ci is not None):
            header, start = (ni, di, ci), i + 1
            break
    if header is None:
        raise tables.StatementError(
            "couldn't find Particulars + Debit/Credit columns — is this a trial balance?")
    ni, di, ci = header
    out = []
    for r in rows[start:]:
        name = str(r[ni]).strip() if ni < len(r) else ""
        if not name or name.lower() in ("total", "grand total"):
            continue
        debit = tables.num(r[di]) if di is not None and di < len(r) else 0.0
        credit = tables.num(r[ci]) if ci is not None and ci < len(r) else 0.0
        if debit == 0 and credit == 0:
            continue
        out.append(LedgerBalance(name, abs(debit), abs(credit)))
    return out


def _classify_rule(name: str):
    for rx, st, grp in _COMPILED:
        if rx.search(name or ""):
            return st, grp
    return None, None


def map_to_schedule3(balances: list, brain=None) -> list:
    """Map each ledger → (statement, group). Rules first; the local model names the unknowns."""
    mapped = []
    unknown = []
    for i, b in enumerate(balances):
        st, grp = _classify_rule(b.name)
        mapped.append({"ledger": b, "statement": st, "group": grp,
                       "basis": "rule" if st else "unmapped"})
        if st is None:
            unknown.append(i)
    if brain and unknown:
        _map_llm(balances, mapped, unknown, brain)
    # anything still unknown: fall back by sign (debit→asset, credit→liability) so it still balances
    for m in mapped:
        if m["statement"] is None:
            b = m["ledger"]
            if b.net >= 0:
                m["statement"], m["group"], m["basis"] = "BS", "Short-term loans & advances", "fallback"
            else:
                m["statement"], m["group"], m["basis"] = "BS", "Other current liabilities", "fallback"
    return mapped


def _map_llm(balances, mapped, idx, brain):
    groups = _BS_LIABS + _BS_ASSETS + _PL_INCOME + _PL_EXPENSE
    items = [{"i": i, "ledger": balances[i].name,
              "side": "debit" if balances[i].net >= 0 else "credit"} for i in idx]
    prompt = ("Map each Indian trial-balance ledger to ONE Schedule III group from this list:\n"
              + ", ".join(groups) + "\nReply ONLY JSON: {\"map\":[{\"i\":<int>,\"group\":\"...\"}]}\n\n"
              + str(items))
    try:
        import json
        raw = brain([{"role": "user", "content": prompt}])
        t = raw.strip().strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
        data = json.loads(t[t.find("{"):t.rfind("}") + 1])
        bygrp = {g.lower(): g for g in groups}
        for m in data.get("map", []):
            g = bygrp.get(str(m.get("group", "")).strip().lower())
            if g is not None:
                st = "PL" if g in (_PL_INCOME + _PL_EXPENSE) else "BS"
                mapped[int(m["i"])].update(statement=st, group=g, basis="llm")
    except Exception:
        return


def build_statements(mapped: list) -> dict:
    """Aggregate mapped ledgers into a Balance Sheet + P&L with Schedule III subtotals."""
    def bucket(groups, statement):
        out = {g: 0.0 for g in groups}
        for m in mapped:
            if m["statement"] == statement and m["group"] in out:
                out[m["group"]] += abs(m["ledger"].net)
        return {g: round(v, 2) for g, v in out.items() if v}

    income = bucket(_PL_INCOME, "PL")
    expense = bucket(_PL_EXPENSE, "PL")
    total_income = round(sum(income.values()), 2)
    total_expense = round(sum(expense.values()), 2)
    pbt = round(total_income - total_expense, 2)

    liabs = bucket(_BS_LIABS, "BS")
    assets = bucket(_BS_ASSETS, "BS")
    liabs["Profit & loss (current year)"] = pbt        # carry P&L into reserves so it balances
    total_liabs = round(sum(liabs.values()), 2)
    total_assets = round(sum(assets.values()), 2)
    return {
        "pnl": {"income": income, "expense": expense, "total_income": total_income,
                "total_expense": total_expense, "profit_before_tax": pbt},
        "bs": {"equity_liabilities": liabs, "assets": assets,
               "total_equity_liabilities": total_liabs, "total_assets": total_assets,
               "balanced": abs(total_liabs - total_assets) < 1.0},
    }


def to_text(st: dict) -> str:
    def block(title, d, total_label, total):
        lines = [title]
        for k, v in d.items():
            lines.append(f"  {k:<38} {v:>16,.2f}")
        lines.append(f"  {'— ' + total_label:<38} {total:>16,.2f}")
        return "\n".join(lines)

    p, b = st["pnl"], st["bs"]
    out = ["STATEMENT OF PROFIT & LOSS",
           block("Income", p["income"], "Total income", p["total_income"]),
           block("Expenses", p["expense"], "Total expenses", p["total_expense"]),
           f"  {'PROFIT BEFORE TAX':<38} {p['profit_before_tax']:>16,.2f}",
           "", "BALANCE SHEET",
           block("Equity & Liabilities", b["equity_liabilities"], "Total", b["total_equity_liabilities"]),
           block("Assets", b["assets"], "Total", b["total_assets"]),
           ("  ✓ balanced" if b["balanced"]
            else f"  ⚠ NOT balanced — diff ₹{b['total_equity_liabilities'] - b['total_assets']:,.2f} "
                 "(check unmapped ledgers)")]
    return "\n".join(out)
