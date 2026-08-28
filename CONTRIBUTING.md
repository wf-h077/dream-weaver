# Contributing to Dream Weaver (造梦者)

Thanks for your interest in contributing! 🎉

This project is a multi-agent AI long-form web novel platform. We welcome PRs of all sizes — typo fixes, new prompt templates, agent improvements, performance optimizations, or new features.

---

## 🚀 Quick start

### 1. Set up

```bash
# Fork & clone
git clone https://github.com/<your-username>/dream-weaver.git
cd dream-weaver

# Install
pip install -r requirements.txt
cp .env.example .env
echo "MOCK_MODE=1" >> .env    # ← no API key needed for development!
```

### 2. Run in mock mode (no LLM required)

```bash
# Two terminals
python mcp_server.py    # port 8001
python app.py          # port 8050
```

Open [http://localhost:8050](http://localhost:8050), register, and walk through the flow with preset data.

`MOCK_MODE=1` answers every LLM call with realistic Chinese web-novel prose — perfect for frontend / UX work without burning tokens.

### 3. With a real LLM (for prompt engineering / agent work)

Add to `.env`:
```ini
MOCK_MODE=0
API_KEY=your_local_gpustack_key
BASE_URL=http://localhost:8000/v1
MINIMAX_API_KEY=your_minimax_key
MINIMAX_BASE_URL=https://api.minimaxi.com/v1
```

---

## 🧪 Testing

```bash
# Unit tests (if available)
pytest tests/ -v

# End-to-end smoke test
python E2E_TEST.md   # follow the steps inside

# Or: just open the UI and click through
```

When you change an agent or prompt:
1. Run the relevant scenario in mock mode first (fast + free)
2. Then test with a real LLM on 1-2 chapters (catch subtle prompt regressions)
3. If you can, test with a long novel (50+ chapters) to catch consistency drift

---

## 📁 Project layout

```
app.py              # FastAPI main service (port 8050)
mcp_server.py       # MCP data service (port 8001)
graph.py            # LangGraph workflow
models.py           # LLM call layer (multi-provider)
prompts.py          # All prompt templates
mock_data.py        # ← Mock mode preset responses

agents/             # 5 specialized agents
  planner.py        # Pitch / concept refinement / blueprint
  writer.py         # Chapter generation
  editor.py         # Risk detection / full-chapter revision
  patcher.py        # Localized risk patches
  reviewer.py       # Long-form companion tools

tools/              # Agent self-service tools (search, read, etc.)
skills/             # Dynamic skills (combat, emotion, dialogue)
static/             # Frontend (vanilla HTML/CSS/JS, no framework)
```

---

## 📐 Coding conventions

- **Python 3.10+** (uses `from __future__ import annotations` and modern type hints)
- **No new heavy dependencies** without discussion — the project intentionally has a small footprint
- **LF line endings** for all source files
- **Backend code** lives next to related code; avoid creating `utils.py` / `helpers.py` black holes
- **Prompts** go in `prompts.py` as constants — keep them versioned and diffable
- **Frontend** is vanilla JS in `static/app.js` (~220KB minified) — no React/Vue; respect the existing patterns

### Where to make changes

| You want to... | Edit |
|---|---|
| Add / improve an agent | `agents/<role>.py` + `prompts.py` |
| Add a new prompt template | `prompts.py` (keep all templates together) |
| Add a new LLM provider | `models.py` + `config.py` |
| Change chapter workflow | `graph.py` |
| Add a UI feature | `static/index.html` + `static/app.js` + `static/style.css` |
| Add a new agent tool | `tools/<name>.py` + register in `agents/writer.py` |
| Mock a new endpoint for testing | `mock_data.py` (add a function + register in `_MOCK_DISPATCH`) |

---

## 🐛 Reporting bugs

Use the [🐛 Bug report](../../issues/new?template=bug_report.md) template. Please:
- Include the error message and stack trace
- Mention your OS, Python version, MOCK_MODE status
- Try to reproduce with `MOCK_MODE=1` first to isolate LLM provider issues

## 💡 Suggesting features

Use the [💡 Feature request](../../issues/new?template=feature_request.md) template. Tell us:
- What problem you're trying to solve
- What you want it to look like
- Whether you'd like to implement it yourself

## ❓ Asking questions

Use the [❓ Question / Help](../../issues/new?template=question.md) template, or open a [Discussion](../../discussions) for anything that isn't a bug or a feature.

---

## 📜 License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
