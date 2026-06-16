"""CA workbench — termind v2.2+.

A chartered accountant handles the most confidential data there is: client PAN, bank
statements, ledgers, ITRs. ICAI's Code of Ethics makes confidentiality a duty, and the DPDP
Act 2023 makes the firm a *Data Fiduciary* — critical personal data must stay in India, with
penalties up to ₹250 crore. Yet every mainstream "AI for CAs" tool is cloud-first: the data
leaves the machine. So a CA's real choice today is *do it by hand* or *take on the liability*.

termind closes that gap. The CA workbench runs the whole workflow — parse, reason, draft,
export — **on the CA's own machine**, with the local model, and seals every action into the
tamper-evident ledger. That ledger is not just an audit nicety here: it is the artifact a CA
can show a client or a regulator to *prove* the data never left the device.

Each section is one module: bank (statement → ledger), scrutiny, gst, notice, finstmt.
All of them are pure-stdlib at the core; heavy parsers (xlsx/pdf) lazy-load into termind's
isolated workspace venv, exactly like the database drivers, and never touch the network.
"""
