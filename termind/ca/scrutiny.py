"""Ledger scrutiny / anomaly pass — CA workbench v2.3.

At finalization (and in audit), a CA "scrutinizes" ledgers: export to Excel, eyeball every line
for the things that bite — round-number provisions, double-postings, weekend entries, sudden
spikes, personal spend hiding in business books, blank narrations. It's slow, manual, and easy
to miss. This is *pure data analysis*, exactly what an agent should do — and the data (a client's
full ledger) is precisely what must never go to a cloud tool.

This runs a battery of DETERMINISTIC checks locally (stdlib + a little statistics), each flag with
a reason a reviewer can act on; the LOCAL model is used only to write a short plain-English opinion
over the findings. Nothing leaves the machine. Reuses bank.py's statement parser for input.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from .bank import Txn, parse_statement  # a ledger row has the same shape as a statement row

# narrations that look personal turning up in a business ledger. Kept deliberately specific —
# a false "personal expense" flag is high-severity, so avoid broad words ('gold' → gold loan,
# 'movie' → movie production, 'holiday' → holiday pay) that catch legitimate business items.
_PERSONAL = re.compile(
    r"\b(jewell?ery|diwali gift|gift to|gift for|salon|spa|vacation|casino|lottery|netflix|"
    r"spotify|grocer(y|ies)|school fees?|tuition fees?|wedding|honeymoon|drawings|personal use|"
    r"personal expense)\b", re.I)


@dataclass
class Flag:
    severity: str            # "high" | "medium" | "low"
    kind: str
    date: str
    narration: str
    amount: float
    detail: str


# ── individual checks ─────────────────────────────────────────────────────────────
def _round_numbers(txns):
    for t in txns:
        a = abs(t.amount)
        if a >= 10000 and a % 10000 == 0:
            sev = "medium" if a % 100000 == 0 else "low"
            yield Flag(sev, "round amount", t.date, t.narration, a,
                       "perfectly round figure — verify it isn't an estimate/provision booked as actual")


def _duplicates(txns):
    groups = {}
    for t in txns:
        key = (round(t.amount, 2), (t.narration or "").strip().lower())
        if key[0] > 0 and key[1]:
            groups.setdefault(key, []).append(t)

    def _d(t):
        try:
            return datetime.strptime(t.date, "%Y-%m-%d")
        except (ValueError, TypeError):
            return None

    for (amt, _narr), grp in groups.items():
        if len(grp) < 2:
            continue
        dated = [(t, _d(t)) for t in grp]
        # identical amount+narration is only suspicious when close in time — entries a month
        # apart are usually legitimate recurring postings (rent, salary, EMI), not double entries.
        for t, dt in dated:
            twin = any(o is not t and (od is None or dt is None or abs((dt - od).days) <= 7)
                       for o, od in dated)
            if twin:
                yield Flag("medium", "possible duplicate", t.date, t.narration, amt,
                           f"same amount + narration within ~a week ({len(grp)}× total) — "
                           "check for a double entry")


def _weekend(txns):
    for t in txns:
        if not t.date:
            continue
        try:
            wd = datetime.strptime(t.date, "%Y-%m-%d").weekday()
        except ValueError:
            continue
        if wd >= 5:
            day = "Saturday" if wd == 5 else "Sunday"
            yield Flag("low", "weekend entry", t.date, t.narration, t.amount,
                       f"dated a {day} — unusual posting day for most businesses")


def _missing_narration(txns):
    for t in txns:
        if not (t.narration or "").strip():
            yield Flag("medium", "missing narration", t.date, t.narration, t.amount,
                       "no narration — a ledger entry without a description fails scrutiny")


def _outliers(txns):
    amts = [abs(t.amount) for t in txns if t.amount]
    if len(amts) < 8:
        return
    mean = sum(amts) / len(amts)
    var = sum((a - mean) ** 2 for a in amts) / len(amts)
    sd = math.sqrt(var)
    if sd == 0:
        return
    cutoff = mean + 3 * sd
    for t in txns:
        if abs(t.amount) > cutoff and abs(t.amount) > mean * 2:
            yield Flag("medium", "unusual spike", t.date, t.narration, abs(t.amount),
                       f"₹{abs(t.amount):,.0f} is far above the ledger average of ₹{mean:,.0f} — investigate")


def _personal(txns):
    for t in txns:
        if _PERSONAL.search(t.narration or ""):
            yield Flag("high", "possible personal expense", t.date, t.narration, t.amount,
                       "looks like a personal expense in business books — disallowable / drawings?")


def _bad_dates(txns):
    this_year = datetime.now().year
    for t in txns:
        if not t.date:
            continue
        try:
            y = datetime.strptime(t.date, "%Y-%m-%d").year
        except ValueError:
            continue
        if y < 2000 or y > this_year + 1:
            yield Flag("medium", "date out of range", t.date, t.narration, t.amount,
                       f"year {y} is outside a sensible accounting period — typo or wrong year?")


_CHECKS = (_round_numbers, _duplicates, _weekend, _missing_narration, _outliers,
           _personal, _bad_dates)
_ORDER = {"high": 0, "medium": 1, "low": 2}


def benford(txns) -> dict:
    """First-significant-digit distribution vs Benford's law — a classic fabrication test."""
    digs = Counter()
    for t in txns:
        a = abs(t.amount)
        if a >= 1:
            d = int(str(int(a))[0])
            if 1 <= d <= 9:
                digs[d] += 1
    n = sum(digs.values())
    expected = {d: math.log10(1 + 1 / d) for d in range(1, 10)}
    chi2 = (sum((digs[d] - expected[d] * n) ** 2 / (expected[d] * n) for d in range(1, 10))
            if n else 0.0)
    return {"n": n, "chi2": round(chi2, 2),
            "observed": {d: round(digs[d] / n, 3) if n else 0 for d in range(1, 10)},
            "expected": {d: round(expected[d], 3) for d in range(1, 10)}}


def scrutinize(txns) -> list:
    """Run every deterministic check and return flags, worst first. (Benford reported separately.)"""
    flags = []
    for check in _CHECKS:
        flags.extend(check(txns))
    bf = benford(txns)
    if bf["n"] >= 50 and bf["chi2"] > 15.5:             # 8 d.o.f., ~0.05 critical value
        flags.append(Flag("medium", "benford deviation", "", "(whole ledger)", 0,
                          f"first-digit distribution deviates from Benford's law (χ²={bf['chi2']}) "
                          f"over {bf['n']} entries — sample for fabricated figures"))
    flags.sort(key=lambda f: (_ORDER.get(f.severity, 9), f.date))
    return flags


def summary(flags: list, txns: list) -> dict:
    kinds = Counter(f.kind for f in flags)
    return {
        "transactions": len(txns),
        "flags": len(flags),
        "high": sum(1 for f in flags if f.severity == "high"),
        "medium": sum(1 for f in flags if f.severity == "medium"),
        "low": sum(1 for f in flags if f.severity == "low"),
        "by_kind": dict(kinds),
        "clean": not flags,
    }


def narrative(flags: list, summ: dict, brain=None) -> str:
    """A short scrutiny opinion. The LOCAL model writes it if available; else a plain rollup."""
    if not flags:
        return "No anomalies surfaced by the automated checks. A manual review is still advised."
    head = (f"{summ['flags']} item(s) flagged across {summ['transactions']} entries "
            f"({summ['high']} high · {summ['medium']} medium · {summ['low']} low).")
    if not brain:
        kinds = ", ".join(f"{k} ({n})" for k, n in summ["by_kind"].items())
        return head + " Themes: " + kinds + "."
    sample = "\n".join(f"- [{f.severity}] {f.kind}: {f.narration} ₹{f.amount:,.0f} — {f.detail}"
                       for f in flags[:20])
    prompt = ("You are a chartered accountant reviewing a ledger-scrutiny report. In 3-4 sentences, "
              "summarize the risk and what to check first. Be specific and sober.\n\n"
              + head + "\n" + sample)
    try:
        return brain([{"role": "user", "content": prompt}]).strip() or head
    except Exception:
        return head


def to_csv(flags: list) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Severity", "Kind", "Date", "Narration", "Amount", "Why it was flagged"])
    for f in flags:
        w.writerow([f.severity, f.kind, f.date, f.narration,
                    f"{f.amount:.2f}" if f.amount else "", f.detail])
    return buf.getvalue()


def scrutinize_file(path: str):
    """Convenience: parse a ledger export and scrutinize it. Returns (txns, flags)."""
    txns = parse_statement(path)
    return txns, scrutinize(txns)
