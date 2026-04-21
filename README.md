# PEX — Paper Explainer

A local Streamlit app that walks you through a research paper one sentence at
a time, with Claude on hand to clarify any word, phrase, or idea along the
way. Upload a PDF, get stuck on a sentence, highlight the part you don't
understand, and ask — all without leaving the page.

## Features

- **Sentence-by-sentence reading** with the source PDF page shown alongside,
  lines of the active sentence highlighted.
- **Q&A grounded in the full paper** — ask freeform questions or pick from
  four preset prompts (*Explain simply*, *Define terms*, *Why does this
  matter?*, *Give an example*).
- **Highlight-to-scope** — select a phrase inside the active sentence and a
  preset will target just that phrase instead of the whole sentence.
- **Navigation**: sentence and page stepping, clickable section outline
  (from the PDF's embedded TOC when present; Claude infers one
  semantically when it isn't), full-text search.
- **In-app source view**: the relevant PDF page is shown in a scrollable
  pane that auto-scrolls to the highlighted lines of the current sentence.
- **Keyboard shortcuts**: `←` / `→` for sentences, `Shift+←` / `Shift+→`
  for pages, `1`–`4` for presets, `q` to focus chat, `Esc` to release
  focus.
- **Studies**: each paper's progress + Q&A history are saved in a single
  `.pex` file under `~/PEX_Studies/`. Auto-saved on every action.

## Quick Start

```bash
git clone https://github.com/kc6800/paper_explainer.git
cd paper_explainer
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Add your Anthropic API key to ~/.env:
#   PEX_CLAUDE_API_KEY=sk-ant-...

streamlit run app.py
```

Open `http://localhost:8501`, upload a PDF on the Home screen, give the
Study a name, click **Create**, and you're reading.

## Documentation

- [`docs/user-guide.md`](docs/user-guide.md) — detailed setup, features,
  keyboard shortcuts, troubleshooting.
- [`docs/architecture.md`](docs/architecture.md) — file layout, `.pex`
  format, PDF extraction pipeline, custom components, AI integration.

## Notes

- Uses `claude-opus-4-7` with adaptive thinking and prompt caching — the
  full paper is cached on the first question so follow-ups are cheap.
- PDF extraction is best-effort. Multi-column layouts and scanned-image
  PDFs without a text layer can produce rough results.
- PEX is a single-user local app; see the architecture doc for the
  tradeoffs of turning it into a hosted service.
