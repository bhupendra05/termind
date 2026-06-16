"""Notice-reply drafting — CA workbench v2.5.

A scrutiny/mismatch notice (GST ASMT-10, DRC-01; Income-Tax 143(2), 143(1), 142(1)) lands and the
CA drafts a point-wise reply under deadline. The reply itself is judgment, but the *structure* —
identify the law and section, restate each allegation, respond point-wise, list enclosures — is
mechanical. The catch: drafting needs the client's actual figures (PAN, turnover, ITC), so it
**cannot** be pasted into a cloud chatbot.

This identifies the notice type with deterministic patterns and assembles a professional reply
skeleton offline; when the LOCAL model is available it drafts the full point-wise body grounded in
the notice text and the facts the CA supplies. Nothing leaves the machine.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import tables


@dataclass
class Notice:
    law: str                 # "GST" | "Income-Tax" | "Unknown"
    kind: str                # human label, e.g. "Scrutiny u/s 143(2)"
    section: str             # "143(2)", "ASMT-10", …
    issues: list = field(default_factory=list)
    amounts: list = field(default_factory=list)


# (regex, law, kind, section). Order matters — most specific first.
_PATTERNS = [
    (r"\bASMT[-\s]?10\b", "GST", "GST scrutiny (ASMT-10)", "ASMT-10"),
    (r"\bDRC[-\s]?01A?\b", "GST", "GST demand (DRC-01)", "DRC-01"),
    (r"\bGSTR[-\s]?3B\b.*\bGSTR[-\s]?1\b|\bGSTR[-\s]?1\b.*\bGSTR[-\s]?3B\b", "GST",
     "GSTR-1 vs GSTR-3B mismatch", "outward mismatch"),
    (r"gstr[-\s]?2a|gstr[-\s]?2b|input tax credit|\bITC\b", "GST", "ITC mismatch (2B vs 3B)", "ITC mismatch"),
    (r"\b143\s*\(\s*2\s*\)", "Income-Tax", "Scrutiny assessment u/s 143(2)", "143(2)"),
    (r"\b142\s*\(\s*1\s*\)", "Income-Tax", "Inquiry before assessment u/s 142(1)", "142(1)"),
    (r"\b143\s*\(\s*1\s*\)", "Income-Tax", "Intimation/adjustment u/s 143(1)", "143(1)"),
    (r"\b26AS\b|\bAIS\b|form\s*16", "Income-Tax", "26AS / AIS mismatch", "26AS mismatch"),
    (r"\b139\s*\(\s*9\s*\)", "Income-Tax", "Defective return u/s 139(9)", "139(9)"),
]

_ISSUE_HINTS = [
    (r"mismatch|difference|discrepan", "a reported difference/mismatch in figures"),
    (r"under[-\s]?report|short\s*payment|underpaid|short\s*paid", "alleged short payment / under-reporting"),
    (r"excess\s*(itc|credit|claim)", "allegedly excess input tax credit claimed"),
    (r"non[-\s]?filing|not\s*filed|failure to file", "non-filing / late filing of a return"),
    (r"cash\s*deposit|high\s*value|sft", "a high-value / cash-deposit transaction"),
]
_AMOUNT = re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d+)?)", re.I)


def classify_notice(text: str) -> Notice:
    t = text or ""
    law, kind, section = "Unknown", "Unidentified notice", ""
    for rx, lw, kd, sec in _PATTERNS:
        if re.search(rx, t, re.I | re.S):              # re.S: cross-line "GSTR-1 … GSTR-3B"
            law, kind, section = lw, kd, sec
            break
    issues = [label for rx, label in _ISSUE_HINTS if re.search(rx, t, re.I | re.S)]
    amounts = []
    for m in _AMOUNT.finditer(t):
        try:
            amounts.append(float(m.group(1).replace(",", "")))
        except ValueError:
            pass
    return Notice(law, kind, section, issues, amounts[:8])


def _skeleton(n: Notice, facts: str) -> str:
    pts = n.issues or ["the matter raised in the notice"]
    body_points = "\n".join(
        f"{i}. With reference to {p}, we submit that the assessee's books and returns are in order. "
        "The supporting reconciliation and documents are enclosed for your kind perusal."
        for i, p in enumerate(pts, 1))
    facts_line = f"\nFacts on record provided: {facts}\n" if facts else ""
    amt = (f"\nThe figure(s) referred to in the notice: "
           + ", ".join(f"₹{a:,.0f}" for a in n.amounts) + "." if n.amounts else "")
    return (
        f"To,\nThe Assessing Officer / Proper Officer\n\n"
        f"Sub: Reply to notice — {n.kind}"
        + (f" [{n.section}]" if n.section else "") + "\n\n"
        "Respected Sir/Madam,\n\n"
        f"This is in response to the captioned notice under {n.law} law. We respectfully submit "
        "our point-wise reply as under:\n\n"
        + body_points + "\n" + amt + facts_line +
        "\nIn view of the above, it is prayed that the notice be dropped and the returns be "
        "accepted as filed. We remain available for any clarification.\n\n"
        "Enclosures:\n  1. Reconciliation statement\n  2. Relevant ledgers / returns\n  3. "
        "Supporting invoices/challans\n\nThanking you,\nYours faithfully,\n"
        "(Authorised Signatory / for the Assessee)")


def draft_reply(text: str, facts: str = "", brain=None) -> dict:
    """Identify the notice and draft a point-wise reply. LOCAL model writes the body if available,
    else a professional skeleton. Returns {notice, draft, by_model}."""
    n = classify_notice(text)
    if not brain:
        return {"notice": n, "draft": _skeleton(n, facts), "by_model": False}
    prompt = (
        "You are a chartered accountant in India drafting a formal, point-wise reply to a tax "
        "notice. Be precise, respectful, and reference the assessee's records. Do NOT invent "
        "figures — use only what is given. Output the reply letter only.\n\n"
        f"Notice type: {n.kind} ({n.law}, {n.section}).\n"
        f"Issues detected: {', '.join(n.issues) or 'see notice text'}.\n"
        f"Figures in notice: {', '.join(f'₹{a:,.0f}' for a in n.amounts) or 'none parsed'}.\n"
        f"Facts the CA supplied: {facts or '(none — keep figure slots as placeholders)'}\n\n"
        f"Notice text:\n{text[:6000]}")
    try:
        draft = brain([{"role": "user", "content": prompt}]).strip()
        return {"notice": n, "draft": draft or _skeleton(n, facts), "by_model": bool(draft)}
    except Exception:
        return {"notice": n, "draft": _skeleton(n, facts), "by_model": False}


def draft_from_file(path: str, facts: str = "", brain=None) -> dict:
    return draft_reply(tables.read_text(path), facts=facts, brain=brain)
