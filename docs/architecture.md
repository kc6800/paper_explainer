# PEX — Architecture / How It Works

This document explains what's inside PEX and how the pieces fit together. For
end-user setup and usage, see [`user-guide.md`](./user-guide.md).

---

## 1. Top-level design

PEX is a **single-user, local** Streamlit app. The user opens a browser at
`localhost:8501`, the Streamlit server runs the Python script on every
interaction, and the UI is the server's current render.

At steady state there are just three things going on:

1. A **Study** (a `.pex` file) holds the source PDF plus the reader's progress
   and Q&A history.
2. The **Reading screen** renders the current sentence, the page it came
   from, and a chat panel for asking questions.
3. The **Claude API** answers questions with the full paper provided as
   context (using prompt caching to keep repeat questions cheap).

No database, no accounts, no server-to-server state.

---

## 2. Tech stack

| Layer | Choice |
|---|---|
| UI | Streamlit |
| PDF parsing & rendering | PyMuPDF (`fitz`) |
| Image overlay (highlights) | Pillow |
| AI | Claude API via the `anthropic` Python SDK (Claude Opus 4.7, adaptive thinking for Q&A, streaming) |
| Structured outputs (semantic section extraction) | Pydantic models passed to `client.messages.parse()` |
| Environment config | `python-dotenv` — reads `~/.env` |
| Storage | ZIP files (`*.pex`) on local disk under `~/PEX_Studies/` |

---

## 3. Project layout

```
paper_explainer/
├── app.py                    # Streamlit entry point + UI + extraction
├── components.py             # Python wrappers for 3 custom components
├── study.py                  # .pex file I/O (create / open / save / delete)
├── requirements.txt
├── .streamlit/
│   └── config.toml           # toolbarMode = "viewer" (hides Deploy/Rerun)
├── frontend/                 # Custom Streamlit components (HTML + JS)
│   ├── sentence_box/index.html
│   ├── preset_bar/index.html
│   ├── keyboard/index.html
│   └── pdf_viewer/index.html
├── docs/
│   ├── user-guide.md
│   └── architecture.md       (← you are here)
├── sample_papers/            # gitignored — user's test PDFs
└── .venv/                    # gitignored — Python virtualenv
```

---

## 4. The Study file format (`.pex`)

A Study is a **ZIP archive** on disk. `study.py` treats the `.pex` extension
as nothing more than "a renamed ZIP". Inside:

```
<study_name>.pex
├── paper.pdf        # the exact source PDF, verbatim
└── state.json       # all the metadata
```

### `state.json` schema (v3, current)

```jsonc
{
  "version": 3,
  "paper_filename": "original_name.pdf",
  "created_at": "2026-04-18T...",
  "updated_at": "2026-04-19T...",

  "sentences":       ["Sentence 1.", "Sentence 2.", ...],
  "paragraph_starts": [0, 5, 19, ...],        // sentence indices
  "sections":        [{ "title": "Intro", "idx": 5, "level": 1 }, ...],

  "sentence_pages":  [                         // per-sentence PDF location
    { "page": 0, "rects": [[x0,y0,x1,y1], ...] },
    ...
  ],

  "idx": 42,                                   // last active sentence
  "qa_by_idx": {
    "42": [
      { "role": "user",      "content": "..." },
      { "role": "assistant", "content": "..." }
    ]
  }
}
```

### Backward compat

Older `.pex` files open cleanly; missing fields default to empty. Features
that depend on missing fields degrade gracefully (no Section list, no PDF
viewer) rather than erroring.

### Atomic writes

Saves use a unique-suffixed temp file (`foo.pex.tmp.<uuid-hex>`) and then an
atomic `os.replace`. This means:

- A crash mid-save can't leave a corrupt `.pex` on disk.
- Two concurrent saves from rapid UI reruns don't collide on the same temp
  filename (without the uuid, they sometimes raced and raised
  `FileNotFoundError` during rename).

All of this lives in `study._write`.

---

## 5. PDF extraction pipeline

Everything structural about a Study is computed once, at Study creation time,
by `extract_structured(pdf_bytes, progress_callback)` in `app.py`. It's a
three-pass pipeline over `page.get_text("dict")` output.

### Pass 1 — per-line collection

For every line on every page, record:

- `text` (joined span text, whitespace-normalized)
- `size` (average font size across spans)
- `bbox` (the line's bounding box on the page, for later highlighting)
- `page` (0-indexed page number)

Also accumulate a global list of all font sizes so we can identify the
*dominant* (i.e. body) font size via `collections.Counter.most_common`.

### Pass 2 — per-page filtering + flagging

For each page:

1. **Determine a footnote cutoff y-coordinate.** A line triggers the cutoff
   if it's in the bottom half of the page **and** either:
   - starts with an explicit footnote marker (`*`, `†`, `‡`, `§`, `¶`) AND is
     slightly smaller than body OR in the bottom third, OR
   - sits in the bottom third of the page AND is clearly smaller than body.

   Everything at or below the lowest cutoff y is dropped.

2. **Drop running heads and page numbers.** Short lines at the extreme top
   (<4% of page height) or extreme bottom (>96%) are removed.

3. **Detect paragraph breaks.** Compute the average line height on the page;
   any kept line whose y-gap from the previous kept line exceeds 80% of that
   average is flagged `starts_para = True`. The first kept line on each page
   is also a paragraph start.

4. **Detect headings.** A line is flagged `is_heading = True` if its font is
   ≥ 1.10× body size and the line is short (<200 chars).

### Pass 3 — assembly + offsets

Lines are joined into a single text string, separated by spaces. Each line's
starting character offset in the joined text is recorded, along with the
piece length (after appending a `.` to un-terminated headings so the sentence
splitter keeps them isolated).

### Sentence splitting

`split_sentences(text)` splits on sentence-ending punctuation followed by
whitespace + a capital/quote/digit. A small list of abbreviations
(`e.g.`, `i.e.`, `et al.`, `Fig.`, ...) prevents false splits.

### Derived structures

With sentences in hand, PEX computes:

- **Sentence offsets**: for each sentence, its start position in the joined
  text (found by re-walking). An extra sentinel at `len(text)` makes range
  lookups easy.
- **Paragraph starts**: every line flagged `starts_para` maps (via its char
  offset) to a sentence index. The set is sorted.
- **Sections**: prefer `doc.get_toc(simple=True)` (the PDF's embedded
  outline); for each `(level, title, page)` triple, find the first sentence
  on that page. If the PDF has no embedded outline, `extract_structured`
  returns an empty sections list and the Study-creation caller falls back
  to `extract_sections_via_llm` (see §8).
- **`sentence_pages`**: for each sentence, find all lines whose char ranges
  overlap it. Group by page; keep the lowest-numbered page (first one
  touched) and its line bboxes. This drives the Reading pane's PDF viewer.

---

## 6. The UI

### Home screen (`render_home`)

Two columns:

- **New Study** — PDF uploader + name field + Create, wrapped in `st.form`
  (so Create submits even if Enter hasn't been pressed on the name input).
  Submission uses a two-rerun pattern:
  1. Click → stash `(pdf_bytes, pdf_name, name)` in `st.session_state`,
     flip `creating=True`, rerun.
  2. On the rerun, all form inputs render as **disabled** while
     `extract_structured(...)` runs with a per-page progress bar, then
     `create_study(...)` writes the `.pex`.

  That second rerun is what gives the user visual feedback that parsing is
  in flight.

- **Open Study** — lists `*.pex` in `~/PEX_Studies/` newest-first, each as a
  row with an **Open** button and a 🗑 button. Deleting uses a
  Confirm/Cancel pair via `st.session_state["confirm_delete"]`.

### Reading screen (`render_reading`)

Three regions:

- **Sidebar** — Close Study, progress readout (`Sentence N of M (P%)`),
  search box, Sections list.
- **Reading pane** (left main column) — Active Sentence box (custom
  component) + rendered PDF page with highlights.
- **Q&A pane** (right main column) — nav buttons (sentence + paragraph),
  preset bar (custom component), chat history, chat input.

Every interaction that mutates `study_state` (sentence change, new Q&A turn,
etc.) immediately calls `autosave()`, which serializes the state + pdf bytes
back into the `.pex`.

---

## 7. Custom Streamlit components

Streamlit's built-in widgets don't cover a few things PEX needs: rendering a
sentence with selectable text, wiring button clicks that respect user
selection, and capturing global keyboard events. These are implemented as
three minimal custom components — each is a small `index.html` file with
inline JS that implements the Streamlit component protocol over
`postMessage`.

All three live under `frontend/` and are exposed to Python from
`components.py`.

### `sentence_box`

Renders the active sentence in a bordered, lightly shaded rectangle. It
listens for `selectionchange` on its document; whenever the user highlights
text, it writes the selection to `localStorage["pex_selection"]` and updates
the Scope indicator beneath the sentence.

### `preset_bar`

Renders four preset-question buttons. On click, reads
`localStorage["pex_selection"]` and sends `{preset, selection, nonce}` back
to Python via `Streamlit.setComponentValue`. A fresh `nonce` on every click
defeats Streamlit's automatic dedup, so rapid clicks don't get dropped.

### `keyboard`

Zero-height iframe whose job is to capture global keydown events. It
installs its listener on `window.parent.document` (re-registering cleanly
on every render so stale listeners don't pile up). Events are filtered:

- Ignored during IME composition, key-repeat, and when focus is in a text
  input/textarea.
- `Escape` is handled even in text inputs — it blurs the active element so
  the other shortcuts work again.
- `ArrowLeft/Right` (with or without Shift), digits `1`–`4`, and `q` get
  mapped to actions and sent back to Python with a `nonce` and the current
  `localStorage["pex_selection"]`.

### `pdf_viewer`

Fixed-height (~620 px) scrollable iframe that displays the current PDF
page and auto-scrolls *within itself* to the highlighted region. Takes
two args: the page rendered as a base64-encoded PNG (with highlights
already composited on top by Python) plus the topmost highlight's native
y-offset in the rendered image. On each render, the iframe updates the
`<img>` src and smooth-scrolls its own scroll container so the highlight
sits ~40 px from the top of the pane. This is done inside the iframe
rather than on the main Streamlit page because a highlight near the
bottom of a tall PDF page can't always be brought into view by scrolling
the outer page — the document just isn't tall enough for that to be
possible.

### Cross-iframe selection bridge

Because the sentence text and the preset buttons live in *different*
iframes, `window.getSelection()` in one can't see a selection in the other.
PEX gets around this with `localStorage` — which same-origin iframes share.
`sentence_box` is the writer; `preset_bar` and `keyboard` are readers.

---

## 8. AI integration

Two places call Claude:

### `ask_claude(paper_text, sentence, history, question)` — Q&A

1. Builds a `system` array with:
   - a short role prompt (no caching)
   - the **full paper text** as a text block marked
     `cache_control: {type: "ephemeral"}`
2. Re-plays the per-sentence chat history as the `messages` array.
3. Appends the user's latest question, scoped with the active sentence.
4. Calls `client.messages.stream(...)` on Claude Opus 4.7 with
   `thinking={"type": "adaptive"}`.
5. Streams text deltas into a Streamlit placeholder (`st.empty()`) as they
   arrive, and returns the concatenated answer.

**Prompt caching** means the first question pays for the paper's full input
tokens (at ~1.25× the usual rate), and every subsequent question in the same
session reads the cache at ~10% of input price. This is why Q&A on the same
Study gets noticeably cheaper after the first question.

**Adaptive thinking** lets Claude decide per-request whether and how much to
think. There's no fixed `budget_tokens` — the model self-moderates.

### `extract_sections_via_llm(sentences)` — semantic section outline

Runs once, at Study creation time, **only when the PDF has no embedded
outline** (`doc.get_toc()` returned nothing). It sends Claude the
numbered sentences and asks for a structured outline:

- Output is validated via `client.messages.parse(...)` against a Pydantic
  schema (`{sections: [{title, idx, level}, ...]}`). Invalid entries
  (out-of-range idx, empty title) are discarded in Python.
- Returns `[]` if no API key is configured or the call errors — Study
  creation continues regardless; the feature is opportunistic.
- Not cached: this is a one-off call per Study, and the result lands in
  `state.json`.

---

## 9. State management

Streamlit reruns the Python script on every interaction. Persistent state
during a session lives in `st.session_state`:

| Key | Meaning |
|---|---|
| `study_path` | `pathlib.Path` to the open `.pex`, or `None` on the Home screen |
| `study_state` | The loaded `state.json` dict (mutated in place during reading; re-persisted by `autosave()`) |
| `paper_bytes` | The raw PDF bytes, kept in memory for the PDF viewer to re-render pages |
| `paper_text` | A joined version of all sentences, sent as context to Claude |
| `_pex_page_cache` | Small LRU (10 entries) of rendered page images — keyed by `(study_path, page, width)` |
| `pending_question` | Set by preset clicks / keyboard shortcuts; consumed by the Q&A pane on the same rerun |
| `creating` + `_pending_study` | Flags for the two-rerun Study creation flow |
| `confirm_delete` | Path of the Study currently pending delete confirmation |

### Auto-save

`autosave()` is called immediately after every state mutation (sentence
change, paragraph jump, Q&A turn append). It calls
`study.save_study(path, state, pdf_bytes)`, which writes a fresh ZIP via the
atomic-rename pattern described in §4.

### Auto-scroll after Q&A

When a new assistant turn is appended, PEX emits a zero-height
`st.components.v1.html` iframe with a one-liner that scrolls the last
`[data-testid="stChatMessage"]` on the parent page into view.

---

## 10. Performance notes

- **Rendering PDF pages**: PyMuPDF rasterizes a page in ~50 ms at our
  default 720 px width. PEX caches the *base* (un-highlighted) render per
  page in session state; drawing the sentence's yellow rectangles on top is
  a fast Pillow op, ~2–5 ms.
- **Prompt caching**: keeps per-question Claude cost roughly proportional to
  the *question* length, not the paper length.
- **`extract_structured`** is single-threaded and linear in total page
  count. For a ~150-page paper this takes a few seconds; the progress bar
  updates per page.
- **State size**: `state.json` is mostly `sentence_pages` (coords rounded to
  1 decimal place) and `qa_by_idx`. Even long papers stay under a few MB.
- **No network polling**: Streamlit's websocket handles all UI interaction,
  and `ask_claude` streams via a single long-lived HTTPS request.

---

## 11. Things we deliberately don't do

- No user accounts. PEX is intended to run on your machine.
- No server-side secret management. The API key is read at import time
  from `~/.env` and stays in the local Python process.
- No database. Studies are flat files; backups are "copy the `.pex`".
- No cloud storage. If PEX ever grew into a hosted service, the upload /
  download / account model would be a new project.
- No retry/queue for failed Claude calls beyond what the SDK does by
  default (exponential backoff on 429/5xx). Errors surface as Streamlit
  error messages.
