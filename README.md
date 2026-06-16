# ⚡ termind

### A local, private AI agent for your terminal **and** a Claude-style web UI — one brain. It chats, writes & runs code, queries your databases, and scans your folders for secrets — and **every action it takes is audited**. $0/query.

[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-2.6-d97757.svg)](https://github.com/bhupendra05/termind/releases)
[![Built on AION](https://img.shields.io/badge/built%20on-AION-8957e5.svg)](https://github.com/bhupendra05/aion)

Most "local AI" setups die at the install step: venvs, Ollama, model pulls, PATH fiddling.
**termind** bootstraps all of it with one script — then drops a neon agent REPL into your
terminal that shows you everything it can do the moment it opens. **v2.0** turns it into a
unified local agent: a coding agent with a leash, a database client that previews destructive
queries before they run, a proactive secret scanner, and a tamper-evident audit ledger of
everything it did — all on-device.

```bash
git clone https://github.com/bhupendra05/termind
cd termind && ./setup.sh        # installs venv + AION + Ollama + model (asks first)
termind                          # ← boots the terminal REPL AND a local web UI
```

`termind` opens **two surfaces sharing one brain**: the cyberpunk terminal REPL *and* a
local Claude-style web chat (auto-opens in your browser). Whatever you teach it, build, or
`/remember` in one shows up in the other. The web UI has a model picker, runs every command,
and binds to **localhost only** — nothing leaves your machine.

- `termind` — terminal + web (default)
- `termind --no-web` — terminal only
- `termind --web` — web UI only

```
  ▀█▀ █▀▀ █▀█ █▀▄▀█ █ █▄░█ █▀▄
  ░█░ ██▄ █▀▄ █░▀░█ █ █░▀█ █▄▀   v2.6.0 · local agent · on AION

┌─ FEATURES ─────────────────────────────────────────────────┐
│  just type        chat with your local model (gemma3)
│  /index · /ask    index your docs · answer with source cites
│  code mode        set a folder → it writes & runs code, jailed
│  /db <nl|sql>     query your DB · preview before destructive ops
│  /scan            sweep the folder for secrets & risky scripts
│  /ca <section>    CA workbench: bank·scrutiny·gst·notice·fs, on-device
│  /ledger          tamper-evident log of every action · export
│  /reach <q>       frontier model on consent — every byte logged
│  /termind ·/tier  isolated workspace · smart/smarter/max
│  /help            show this again        /exit  quit
└────────────────────────────────────────────────────────────┘
  private · $0/query · sandboxed & budgeted by the AION kernel

termind ❯ /db delete users who never logged in
⚠ DESTRUCTIVE on 'app':
  DELETE FROM users WHERE last_login IS NULL
  preview: 1284 row(s) affected · plan: SCAN users
  reply 'confirm' to run it, or 'cancel'. (Nothing has changed.)
```

## Why it's different
- 🚀 **One-command setup** — `./setup.sh` handles the venv, [AION](https://github.com/bhupendra05/aion), Ollama, and the model (always asking before installing anything). Optional shell alias so `termind` works in every new terminal.
- 🔒 **Private by default, frontier on consent** — a local model (Gemma via Ollama) answers everything on-device. When a task is too hard, `/reach` escalates *one* step to a frontier model **only if you ask** — and every byte that leaves the machine is logged.
- 🧾 **Auditable** — every action (file write, shell run, DB query, cloud escalation) is sealed into a **tamper-evident, hash-chained ledger** you can verify offline and export. `/status` and `/ledger` show the truth, including exact bytes off-machine.
- 🗄️ **Databases, safely** — connect SQLite (zero-dependency), Postgres, MySQL, or MongoDB; ask in plain English or SQL. termind **verifies every query** and shows an `EXPLAIN` plan + exact affected-row count **before** any destructive op — and won't run it until you confirm.
- 🛡️ **Proactive security scanning** — the moment you select a folder, termind sweeps it (offline) for exposed secrets, dangerous scripts (`curl | sh`, `rm -rf /`), and insecure deps, with file:line and a fix.
- 🧮 **CA workbench (v2.2–v2.6)** — a full tab for chartered accountants, who legally *can't* put client data in cloud tools (ICAI confidentiality + DPDP Act 2023). Five sections, all on-device with the local model, each parse + export sealed into the ledger as your "data never left the machine" proof:
  - `/ca bank` — bank statement (CSV/Excel/PDF) → ready-to-import **Tally vouchers** + review CSV (rules first, local model for the rest)
  - `/ca scrutiny` — ledger **anomaly pass**: round numbers, duplicates, weekend entries, spikes, Benford, possible personal expenses
  - `/ca gst` — **GSTR-2B vs purchase register** reconciliation → ITC-at-risk, unbooked ITC, value mismatches, invoice-no typos
  - `/ca notice` — identify a GST/Income-Tax notice (143(2), ASMT-10, …) → drafted **point-wise reply**
  - `/ca fs` — trial balance → **Schedule III** Balance Sheet + P&L with a balance check
- 👨‍💻 **A coding agent with a leash** — set a folder and it writes & runs real code in an act-observe loop, jailed to that workspace, with plan / act / bypass modes. Gathers all decisions in one first turn before building — no mid-task interruptions.
- ⚡ **Real model tiers** — smart (local), smarter (bigger local via `TERMIND_BIG_MODEL`), max (frontier auto-selected on every call, every call logged). Cycle with one click on the context bar.
- 🧰 **Bottom context bar** — folder, active DB, agent mode, model, and tier live at the bottom of the chat input (like Claude Code), always visible. One glance tells you the full agent context.
- 🧹 **Disposable & contained** — drivers and scratch assets install into an isolated workspace venv; `/termind cleanup` prints a complete uninstall plan.

## Tests
```bash
.venv/bin/python -m pytest -q     # 207 tests
```
The suite covers retrieval with source cites, the code agent's act-observe loop, the AION
sandbox guarantee (a rogue agent's `fs.write` is denied by the kernel), the audit ledger's
tamper-evidence, the database **preview-before-destructive** gate, the security scanner, and
the full CA workbench (Tally XML signs, scrutiny anomaly checks, GST reconciliation buckets,
notice classification, and Schedule III mapping that balances).

## Family
[**AION**](https://github.com/bhupendra05/aion) (the agent OS) · [**localmind**](https://github.com/bhupendra05/localmind) (private RAG agent) · **termind** (the terminal experience).

MIT © Bhupendra Tale
