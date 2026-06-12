"""termind's built-in knowledge base — the support bot answers FROM these docs.

Honest, self-contained answers about what termind can do, its rules (e.g. the GGUF
criteria for custom models), workflows, and limitations. Works even offline: if no
model is running, the best-matching topic is returned directly.
"""

TOPICS = {
    "what is termind": (
        "termind is a local AI agent that runs on YOUR machine — terminal + a web chat "
        "sharing one brain. It chats (Ollama models like gemma3), remembers you across "
        "sessions, answers from your own documents (/index + /ask), builds projects from a "
        "sentence (/build), runs consented shell commands (/do), and sees/edits images. "
        "Private: localhost only, $0 per query, every action sandboxed and budgeted by the "
        "AION kernel."),
    "custom model criteria": (
        "Bring your own model — the rules:\n"
        "• File format MUST be GGUF (the llama.cpp/Ollama standard). One file, quantized "
        "(e.g. Q4_K_M) recommended.\n"
        "• Import a local file: /import ~/models/your-model.gguf [name] — or paste the path "
        "in ⚙ Models → 'bring YOUR OWN model'.\n"
        "• From Hugging Face: /pull hf.co/<user>/<repo> works for any GGUF repo.\n"
        "• Remote machine: launch with OLLAMA_HOST=http://host:11434 termind.\n"
        "• If your fine-tune is still PyTorch/safetensors, convert once with llama.cpp's "
        "convert_hf_to_gguf.py (tools like Unsloth can export GGUF directly).\n"
        "• Size guidance: 2-5 GB models run great on 16GB+ RAM; bigger needs more."),
    "model downloads": (
        "⚙ Models (web) shows installed brains and a curated catalog with sizes and what "
        "each is good at: gemma3 = all-rounder + vision, qwen2.5 = coding, deepseek-r1 = "
        "reasoning, moondream = tiny vision. One click downloads with a progress bar; "
        "'use' switches instantly. Terminal: /model to list+switch, /pull <name> to "
        "download. Your choice is remembered."),
    "memory and privacy": (
        "termind remembers facts you tell it (/remember or naturally: 'I am…', 'I prefer…'), "
        "indexed documents, and your conversations — all in ONE local file "
        "(~/.termind/memory.json). Nothing is uploaded anywhere, ever. The web UI binds to "
        "localhost only. Settings lets you import memories pasted from other AI platforms, "
        "export yours, or wipe them. /recall searches everything by meaning."),
    "workflows": (
        "Common workflows:\n"
        "• Build a tool: 'create a new project: a pomodoro timer in python' → plan → folder "
        "→ code → ARCHITECTURE.md → VS Code opens → it runs.\n"
        "• Ask your documents: /index ~/notes then /ask what are my action items?\n"
        "• Image Q&A: attach an image (📎 web, /img terminal) and ask about it.\n"
        "• Edit images: 'make it brighter and b&w', 'remove background', 'remove the logo "
        "in the top right' (say WHERE for best results).\n"
        "• Hard questions: /think <question> escalates to the strongest available brain.\n"
        "• Shell help: /do show me the largest files here (runs only after your y)."),
    "limitations": (
        "Honest limits:\n"
        "• Local models are small (2-8GB): great at chat/summaries/simple code, weaker at "
        "long multi-step reasoning than cloud frontier models. /think helps.\n"
        "• Image editing = deterministic ops + neural background removal + LaMa object "
        "removal. It reconstructs backgrounds; it does NOT do generative fill (no 'replace "
        "the sky with a dragon').\n"
        "• Object removal works best when YOU say where ('in the top right') — small models "
        "are bad at pixel coordinates, so termind trusts your words first.\n"
        "• One user per machine (the profile is local, not an account system).\n"
        "• Custom models must be GGUF format."),
    "settings and profile": (
        "Settings (web ⚙) covers: your profile (name, role, how you like answers — all fed "
        "to the model so replies fit you), theme (dark / light / cyberpunk), memory tools "
        "(import pasted memories from ChatGPT/Claude exports, export yours, clear), and "
        "this help. Terminal: /profile shows yours; the onboarding page appears on first "
        "run to set it up."),
}

DOC = "\n\n".join(f"## {k}\n{v}" for k, v in TOPICS.items())


STOP = {"what", "are", "is", "the", "a", "an", "your", "my", "i", "do", "does", "how",
        "can", "you", "to", "of", "in", "and", "or", "it", "this", "for", "with", "into"}


def best_topic(query: str) -> str:
    """Offline help: best topic by meaningful-word overlap (title hits count 5x)."""
    import re
    q = {w for w in re.findall(r"[a-z]+", query.lower()) if w not in STOP}

    def score(kv):
        title = set(kv[0].lower().split())
        body = set(re.findall(r"[a-z]+", kv[1].lower()))
        return 5 * len(q & title) + len(q & body)

    k, v = max(TOPICS.items(), key=score)
    return f"{k}:\n{v}"
