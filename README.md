# PEX — Paper Explainer

A Streamlit app that walks you through a research paper one sentence at a time,
with Claude on hand to explain any word, phrase, or idea along the way.

## Setup

```bash
cd paper_explainer
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
```

## Run

```bash
streamlit run app.py
```

## Studies

PEX organizes work into **Studies**. A Study is a single `.pex` file that
bundles the source PDF together with your progress (current sentence + Q&A
history). Studies are auto-saved on every action, so you never have to think
about Ctrl+S.

On launch, PEX shows a **Home** screen with two options:

- **New Study** — upload a PDF, give the Study a name, click Create.
- **Open Study** — resume from the list of recent Studies.

All Studies live in `~/PEX_Studies/` by default. A `.pex` file is just a ZIP
archive containing `paper.pdf` + `state.json`, so you can inspect or back one
up with any standard tool.

## Reading

Use **Previous / Next** (or the jump box in the sidebar) to move through the
paper one sentence at a time. The sentences above and below the current one are
shown dimmed for context. Ask questions in the right-hand pane — each answer is
grounded in the full paper, and the Q&A history is kept per-sentence so you can
return to an earlier spot and still see what you asked.

## Notes

- Uses `claude-opus-4-7` with adaptive thinking.
- The full paper text is sent with prompt caching so repeat questions are cheap.
- PDF text extraction is best-effort; multi-column layouts and heavy math may
  produce rough sentence splits.
