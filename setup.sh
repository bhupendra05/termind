#!/usr/bin/env bash
# termind one-command bootstrap: ./setup.sh
# Creates a venv, installs termind + AION, installs Ollama + a local model (with your
# permission), and offers a shell alias so `termind` works from any terminal.
set -euo pipefail

C='\033[0;36m'; M='\033[0;35m'; G='\033[0;32m'; Y='\033[1;33m'; N='\033[0m'
here="$(cd "$(dirname "$0")" && pwd)"
MODEL="${TERMIND_MODEL:-gemma3}"

echo -e "${M}▲ termind setup${N} — local AI agent for your terminal\n"

# 1. python + venv
command -v python3 >/dev/null || { echo -e "${Y}python3 is required — install it and re-run.${N}"; exit 1; }
echo -e "${C}[1/4]${N} creating virtualenv + installing termind (+ AION)…"
python3 -m venv "$here/.venv"
"$here/.venv/bin/python" -m pip install -U pip -q
"$here/.venv/bin/python" -m pip install -e "$here" -q
echo -e "      ${G}✓ installed${N}"

# 2. ollama
echo -e "${C}[2/4]${N} checking for Ollama (runs the local model)…"
if command -v ollama >/dev/null; then
  echo -e "      ${G}✓ ollama found${N}"
else
  echo -e "      ${Y}ollama not found.${N} Install it now? [y/N]"
  read -r ans
  if [[ "${ans:-n}" =~ ^[Yy] ]]; then
    if [[ "$(uname)" == "Darwin" ]] && command -v brew >/dev/null; then
      brew install ollama
    else
      curl -fsSL https://ollama.com/install.sh | sh
    fi
    echo -e "      ${G}✓ ollama installed${N}"
  else
    echo -e "      skipped — termind will run with its offline brain until you install it."
  fi
fi

# 3. model
echo -e "${C}[3/4]${N} checking for the ${MODEL} model…"
if command -v ollama >/dev/null; then
  (ollama list 2>/dev/null | grep -q "$MODEL") && echo -e "      ${G}✓ $MODEL ready${N}" || {
    echo -e "      pulling ${MODEL} (one-time download)…"
    ollama pull "$MODEL" || echo -e "      ${Y}pull failed — start the Ollama app and re-run, or use the offline brain.${N}"
  }
else
  echo -e "      skipped (no ollama)"
fi

# 4. alias so it's available in every terminal
echo -e "${C}[4/4]${N} add a ${M}termind${N} alias to your shell so it works in any terminal? [y/N]"
read -r ans2
rc="$HOME/.zshrc"; [[ "${SHELL:-}" == *bash* ]] && rc="$HOME/.bashrc"
if [[ "${ans2:-n}" =~ ^[Yy] ]]; then
  line="alias termind=\"$here/.venv/bin/termind\""
  grep -qF "$line" "$rc" 2>/dev/null || echo "$line" >> "$rc"
  echo -e "      ${G}✓ added to $rc${N} (open a new terminal and type: termind)"
fi

echo -e "\n${G}✓ setup complete.${N} run it now:\n   ${M}$here/.venv/bin/termind${N}\n"
