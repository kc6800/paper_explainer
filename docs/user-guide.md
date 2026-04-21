# PEX — User Guide

PEX (Paper Explainer) is a desktop web app that walks you through a research
paper one sentence at a time, with Claude on hand to clarify any word, phrase,
or idea along the way. You read the paper sentence-by-sentence; whenever you
get stuck, you ask — and the AI answers in the context of the full paper.

---

## Requirements

| | |
|---|---|
| Python | 3.9 or newer |
| Operating system | macOS, Linux, or Windows (tested on macOS) |
| Disk | ~200 MB for Python dependencies + your `.pex` Study files |
| Network | Required — PEX talks to the Claude API |
| Anthropic API key | Required (see below) |

You will also need a PDF reader to inspect source documents outside the app
(PEX shows pages inside the app, so this is optional).

---

## Installation

1. **Clone the repo.**
   ```bash
   git clone https://github.com/kc6800/paper_explainer.git
   cd paper_explainer
   ```

2. **Create a virtual environment** so PEX's dependencies don't pollute your
   system Python.
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   ```

3. **Install dependencies.**
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

   This pulls in `streamlit` (the UI framework), `anthropic` (the Claude API
   SDK), `pymupdf` (PDF parsing/rendering), and `python-dotenv` (env-file
   loader). It also brings in Pillow as a Streamlit dependency, which PEX uses
   for highlight overlays.

---

## API Key Setup

PEX reads your Anthropic API key from a local `.env` file under the key name
`PEX_CLAUDE_API_KEY`. (If it's missing, PEX also accepts the standard
`ANTHROPIC_API_KEY` as a fallback.)

1. Go to https://console.anthropic.com/settings/keys and click **Create Key**.
2. Copy the key — it starts with `sk-ant-...`.
3. Add it to `~/.env` (a file in your home directory):
   ```
   PEX_CLAUDE_API_KEY=sk-ant-...
   ```
   If the file doesn't exist, create it.
4. Make sure you've funded the key under **Billing → Plans & Billing**. A
   small amount (~$5) goes a long way for PEX-style use.

> PEX never uploads your key or your papers anywhere except the Claude API.

---

## Running PEX

From inside the project directory, with the virtual environment activated:

```bash
streamlit run app.py
```

Streamlit will print a local URL (usually `http://localhost:8501`). Open it in
your browser.

To stop the app, press **Ctrl-C** in the terminal where Streamlit is running.

---

## The Home Screen

When you open PEX with no Study loaded, the Home screen shows two panels:

- **New Study** (left) — upload a PDF and start a fresh reading.
- **Open Study** (right) — resume one of your saved Studies.

### Creating a new Study

1. In the **New Study** panel, click **PDF** and choose a `.pdf` file.
2. Type a **Name** for the Study (e.g., `Attention Is All You Need`).
3. Click **Create**.

PEX will show a progress bar while it parses the PDF. Three things happen:

- **Text extraction** — it scans every page, drops page numbers / running
  heads, and filters out footnotes so they don't interleave with the body.
- **Structure detection** — paragraph breaks are inferred from line spacing;
  section headings come from the PDF's embedded outline when the PDF
  provides one. If it doesn't, PEX falls back to a **semantic extraction**
  (one Claude call) that reads the numbered sentences and returns a
  structured outline. That step is labeled
  *"No embedded outline — detecting sections with Claude…"* in the progress
  area, and adds a few seconds plus a small API cost to Study creation.
- **Per-sentence page mapping** — every sentence is tagged with the page it
  appears on and the line bounding boxes to highlight.

When it's done, PEX switches to the Reading screen. The Study is saved to
`~/PEX_Studies/<name>.pex`.

### Opening an existing Study

In the **Open Study** panel, click the name of a Study to resume it. The Study
list is sorted by most-recently-opened.

### Deleting a Study

Click the 🗑 button to the right of a Study in the list. PEX will show a
**Confirm delete** / **Cancel** pair — confirming permanently removes the
`.pex` file (Q&A history and all).

---

## The Reading Screen

Layout:

```
┌─ Sidebar ────────────┬─ Reading pane ─────┬─ Q&A pane ────────┐
│ PEX v1.0             │                    │ ← Previous Next → │
│ ← Close Study        │  ┌──────────────┐  │ ⟵ Page · Page ⟶  │
│ <Study name>         │  │ Active       │  │                   │
│ Sentence 13 / 1300   │  │ sentence     │  │ [Explain] [Def.]  │
│ [██░░░░░] 1%         │  │ (bordered)   │  │ [Why?]   [Ex.]    │
│                      │  └──────────────┘  │                   │
│ Search: [____]       │                    │ Ask about this    │
│                      │  ┌──────────────┐  │ sentence:         │
│ Sections             │  │              │  │                   │
│   Introduction       │  │   Source     │  │   ● You asked X   │
│   Methods            │  │   PDF page   │  │   ● PEX answered  │
│   Results            │  │   w/ line    │  │                   │
│   Discussion         │  │   highlights │  │ [ What's unclear? ]│
│                      │  │              │  │                   │
└──────────────────────┴─ (page N) ────────┴───────────────────┘
```

### Reading pane (center)

- **Active sentence box** — the current sentence in a bordered, lightly
  shaded rectangle. Select text inside to scope preset questions to just that
  phrase (see below).
- **Source PDF viewer** — the original page the sentence comes from, rendered
  as an image, with the sentence's line(s) highlighted in soft yellow. The
  viewer is a fixed-height scrollable pane that auto-scrolls internally to
  the highlight on every sentence change, so the pane's position on the
  overall page never jumps around.

### Q&A pane (right)

- **Navigation buttons**: sentence-level and page-level.
- **Preset bar**: four canned questions — *Explain simply*, *Define terms*,
  *Why does this matter?*, *Give an example*. Each uses either your selected
  text (if any) or the full sentence as the scope.
- **Chat history** — the conversation for the *current* sentence. Q&A is kept
  per-sentence, so jumping back to an earlier sentence shows what you asked
  there.
- **Chat input** — for freeform questions.

### Sidebar (left)

- **Close Study** — returns to the Home screen.
- **Progress** — shows *Sentence N of M (P%)* with a progress bar.
- **Search** — type a word or phrase; PEX shows matching sentences with
  snippets. Click a match to jump there.
- **Sections** — a clickable outline. Click any entry to jump to the first
  sentence of that section.

---

## Asking Questions

Two ways to ask:

- **Freeform** — type in the chat input at the bottom of the Q&A pane and
  press Enter. Claude answers with the full paper as context.
- **Preset** — highlight a word or phrase in the Active sentence box, then
  click one of the four preset buttons (or press 1–4). If nothing is
  highlighted, the whole sentence is used as the scope.

Answers are streamed in real time and saved automatically as part of the
Study. The newest answer auto-scrolls into view.

> PEX uses Claude's **prompt caching** — the full paper text is cached on
> Anthropic's side, so repeat questions on the same Study are much cheaper
> than the first one.

---

## Keyboard Shortcuts

| Key | Action |
|---|---|
| `→` | Next sentence |
| `←` | Previous sentence |
| `Shift + →` | Next page (first sentence on the next source PDF page) |
| `Shift + ←` | Previous page |
| `1` | Ask preset 1 — Explain simply |
| `2` | Ask preset 2 — Define terms |
| `3` | Ask preset 3 — Why does this matter? |
| `4` | Ask preset 4 — Give an example |
| `q` | Focus the chat input (start typing a question) |
| `Esc` | Release focus from a text input (re-enables the other shortcuts) |

Shortcuts are **disabled while you're typing** in the chat input or the
search box. Press `Esc` to get out of a text input; your shortcuts will
work again.

---

## Studies (the `.pex` file)

Each Study is a single `.pex` file in `~/PEX_Studies/`. Under the hood it's a
ZIP archive containing the source PDF plus a `state.json` with your progress
and Q&A history. You can back up a Study by copying the `.pex` file, or share
it by sending the file.

PEX **auto-saves** on every meaningful action — sentence change, question
asked, etc. You never need to save manually.

---

## Troubleshooting

**The Create button does nothing.**
Make sure you've both uploaded a PDF *and* entered a name.

**Parsing a PDF fails or produces garbled text.**
Some PDFs are scanned images without a text layer (pure OCR PDFs sometimes
qualify). PEX can't read those. Try running the PDF through an OCR tool that
embeds a real text layer first.

**Footnotes still appear in the body.**
PEX uses conservative heuristics (marker `*†‡§¶` + small font or bottom-third
position). If a paper embeds footnotes with no distinguishing signals, some
may leak through. Send the offending sentence to the project maintainer so
the heuristics can be tightened.

**"No API key found."**
Check that `~/.env` exists and has a line `PEX_CLAUDE_API_KEY=sk-ant-...` with
no quotes around the key. Restart the Streamlit server after editing.

**The Sections list is empty.**
The PDF has no embedded outline and the semantic (LLM) fallback either
wasn't run (no API key at Study creation) or didn't find anything.
Search still works; you can navigate by sentence and paragraph.

**The PDF viewer shows a message instead of the page.**
The Study was created with an older version of PEX that didn't track
per-sentence page locations. Delete the Study and re-create it from the PDF
to enable the viewer.

**The app says a Study with that name already exists.**
PEX won't overwrite existing Studies. Either pick a different name or delete
the old Study first.

**Keyboard shortcuts stopped working.**
Your focus is probably in the chat input or another text field. Press `Esc`
to release focus, then try again.

---

## Limitations (current version)

- Sentences that span a page break are shown on the first page only.
- Two-column or multi-column layouts can occasionally produce out-of-order
  text if PyMuPDF's reading-order algorithm misreads the columns.
- The Claude API is billed per token — long papers with lots of questions
  can add up. Prompt caching keeps repeat questions cheap.
- PEX is a local app. For multi-user or shared-state deployment, the Study
  storage model would need rework.
