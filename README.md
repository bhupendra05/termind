# ⚡ termind

### A local AI agent that lives in your terminal. **Clone it, run one command, done.**

[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Built on AION](https://img.shields.io/badge/built%20on-AION-8957e5.svg)](https://github.com/bhupendra05/aion)

Most "local AI" setups die at the install step: venvs, Ollama, model pulls, PATH fiddling.
**termind** bootstraps all of it with one script — then drops a neon agent REPL into your
terminal that shows you everything it can do the moment it opens.

```bash
git clone https://github.com/bhupendra05/termind
cd termind && ./setup.sh        # installs venv + AION + Ollama + model (asks first)
termind                          # ← a local agent, in any terminal
```

```
  ▀█▀ █▀▀ █▀█ █▀▄▀█ █ █▄░█ █▀▄
  ░█░ ██▄ █▀▄ █░▀░█ █ █░▀█ █▄▀   v0.3.0 · local agent · on AION

┌─ FEATURES ─────────────────────────────────────────────────┐
│  just type        chat with your local model (gemma3)
│  /index <folder>  index your notes/docs/code (stays local)
│  /ask <question>  answer from YOUR docs, with source cites
│  /build <idea>    scaffold project · code · VS Code · run
│  or just say it   "create a folder x" · "build a tool…"
│  /status          model · credits spent · sandbox audit
│  /help            show this again        /exit  quit
└────────────────────────────────────────────────────────────┘
  private · $0/query · sandboxed & budgeted by the AION kernel

termind ❯ /ask what are the action items from my notes?
From your notes (standup.md#1): Email the investor list and review the RAG pipeline.
```

## Why it's different
- 🚀 **One-command setup** — `./setup.sh` handles the venv, [AION](https://github.com/bhupendra05/aion), Ollama, and the model (always asking before installing anything). Optional shell alias so `termind` works in every new terminal.
- 🔒 **Private & $0** — a local model (Gemma via Ollama); your docs are indexed and queried on-device. Nothing leaves the machine.
- 🛡️ **Sandboxed & budgeted** — every `/ask` runs as an AION process with least-privilege capabilities and a hard credit budget. `/status` shows the audit.
- 🧰 **Works before the model exists** — no Ollama yet? An offline brain keeps the REPL alive and `/index` + `/ask` still do *real* retrieval.

## Tests
```bash
.venv/bin/python -m pytest -q
```
The suite covers retrieval with source cites, command handling, and the sandbox guarantee
(a rogue agent's `fs.write` is denied by the kernel).

## Family
[**AION**](https://github.com/bhupendra05/aion) (the agent OS) · [**localmind**](https://github.com/bhupendra05/localmind) (private RAG agent) · **termind** (the terminal experience).

MIT © Bhupendra Tale
