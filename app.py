"""PEX — Paper Explainer.

Streamlit app that walks you through a research paper one sentence at a time,
with Claude available to clarify words, phrases, or ideas on demand.
"""

from __future__ import annotations

import html
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

APP_VERSION = "1.0"
CONTEXT_WINDOW = 5

import anthropic
import fitz  # pymupdf
import streamlit as st
from dotenv import load_dotenv

from components import keyboard, preset_bar, sentence_box
from study import (
    STUDIES_DIR,
    create_study,
    delete_study,
    list_studies,
    open_study,
    save_study,
)


PRESETS = {
    "explain_simply": "Explain this in plain, simple language: \u201C{scope}\u201D",
    "define_terms": "Define the key terms or jargon in: \u201C{scope}\u201D",
    "why_matters": "Why does this matter in the context of the paper? \u201C{scope}\u201D",
    "give_example": "Give a concrete example to illustrate: \u201C{scope}\u201D",
}


def build_preset_question(preset: str, scope: str) -> str:
    template = PRESETS.get(preset, "Tell me more about: \u201C{scope}\u201D")
    return template.format(scope=scope)


def _search_sentences(
    sentences: list[str], query: str, max_snippet: int = 80
) -> list[tuple[int, str]]:
    q = query.strip().lower()
    if not q:
        return []
    hits: list[tuple[int, str]] = []
    for i, s in enumerate(sentences):
        lo = s.lower()
        pos = lo.find(q)
        if pos < 0:
            continue
        start = max(0, pos - 20)
        end = min(len(s), pos + len(q) + max_snippet)
        snippet = s[start:end]
        if start > 0:
            snippet = "…" + snippet
        if end < len(s):
            snippet = snippet + "…"
        hits.append((i, snippet))
    return hits


def _prev_para(paragraph_starts: list[int], idx: int) -> int | None:
    candidates = [p for p in paragraph_starts if p < idx]
    return max(candidates) if candidates else None


def _next_para(paragraph_starts: list[int], idx: int) -> int | None:
    candidates = [p for p in paragraph_starts if p > idx]
    return min(candidates) if candidates else None

load_dotenv(Path.home() / ".env")
API_KEY = os.environ.get("PEX_CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")


MODEL = "claude-opus-4-7"

SYSTEM_PROMPT = (
    "You help the user understand a research paper one sentence at a time. "
    "The full paper text is provided for context. When the user asks about a "
    "word, phrase, or idea, give a clear, concise explanation grounded in the "
    "paper. Prefer plain language. If a term is defined elsewhere in the paper, "
    "quote the relevant passage briefly. If something is unclear or ambiguous in "
    "the paper itself, say so."
)


# ---------- PDF + sentence handling ----------

_FOOTNOTE_MARKER = re.compile(r"^\s*[*\u2020\u2021\u00a7\u00b6]\s*\S")


def extract_structured(pdf_bytes: bytes, progress_callback=None) -> dict:
    """Extract body text plus structural info (paragraphs, sections) from a PDF.

    Returns a dict with:
      - `text`: joined body text
      - `sentences`: list of sentences
      - `paragraph_starts`: sorted list of sentence indices that begin a
        paragraph (always starts with 0)
      - `sections`: list of {title, idx, level} — prefers PDF outline
        (`doc.get_toc`); falls back to font-size heading detection

    Footnotes and page furniture are filtered with the same heuristics used
    previously.
    """
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        total_pages = len(doc)
        toc = doc.get_toc(simple=True) or []  # [[level, title, 1-indexed page], ...]

        # Pass 1 — collect per-line info.
        page_data: list[tuple[float, list[dict]]] = []
        sizes: list[float] = []
        for page_num, page in enumerate(doc):
            page_h = page.rect.height
            data = page.get_text("dict")
            lines: list[dict] = []
            for block in data.get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    spans = line.get("spans", [])
                    if not spans:
                        continue
                    raw = "".join(s.get("text", "") for s in spans)
                    text = re.sub(r"\s+", " ", raw).strip()
                    if not text:
                        continue
                    avg_size = sum(s.get("size", 0.0) for s in spans) / len(spans)
                    y0, y1 = line["bbox"][1], line["bbox"][3]
                    lines.append({
                        "text": text,
                        "size": avg_size,
                        "y0": y0,
                        "y1": y1,
                        "page": page_num,
                    })
                    sizes.append(round(avg_size, 1))
            page_data.append((page_h, lines))
            if progress_callback:
                progress_callback((page_num + 1) / total_pages)

        body_size = Counter(sizes).most_common(1)[0][0] if sizes else 10.0

        # Pass 2 — per page, drop footnotes/furniture, flag paragraph breaks + headings.
        flat_lines: list[dict] = []
        for page_h, lines in page_data:
            cutoff_y = page_h
            for line in lines:
                y_frac = ((line["y0"] + line["y1"]) / 2) / page_h
                if y_frac <= 0.45:
                    continue
                is_marker = bool(_FOOTNOTE_MARKER.match(line["text"]))
                slightly_smaller = line["size"] < body_size * 0.98
                in_bottom_third = y_frac > 0.65
                clearly_smaller = line["size"] < body_size * 0.92
                if is_marker and (slightly_smaller or in_bottom_third):
                    cutoff_y = min(cutoff_y, line["y0"])
                elif y_frac > 0.70 and clearly_smaller:
                    cutoff_y = min(cutoff_y, line["y0"])

            kept: list[dict] = []
            for line in lines:
                if line["y0"] >= cutoff_y:
                    continue
                y_frac = ((line["y0"] + line["y1"]) / 2) / page_h
                if (y_frac < 0.04 or y_frac > 0.96) and len(line["text"]) < 60:
                    continue
                kept.append(line)

            if kept:
                heights = [l["y1"] - l["y0"] for l in kept]
                avg_h = sum(heights) / len(heights)
                for i, line in enumerate(kept):
                    line["is_heading"] = (
                        line["size"] > body_size * 1.10
                        and len(line["text"]) < 200
                    )
                    if i == 0:
                        line["starts_para"] = True
                    else:
                        line["starts_para"] = (
                            line["y0"] - kept[i - 1]["y1"] > avg_h * 0.8
                        )
                flat_lines.extend(kept)

        # Pass 3 — join and record per-line char offsets.
        pieces: list[str] = []
        line_offsets: list[tuple[int, dict]] = []
        for line in flat_lines:
            if pieces:
                pieces.append(" ")
            offset = sum(len(p) for p in pieces)
            line_offsets.append((offset, line))
            txt = line["text"]
            # Ensure headings terminate so the sentence splitter doesn't fuse
            # them with the next body sentence.
            if line.get("is_heading") and not txt.rstrip().endswith(
                (".", "!", "?", ":")
            ):
                txt = txt + "."
            pieces.append(txt)

        text = "".join(pieces).strip()

    sentences = split_sentences(text)

    # Compute char offsets for each sentence by re-walking the text.
    sentence_offsets: list[int] = []
    pos = 0
    for s in sentences:
        found = text.find(s, pos)
        if found == -1:
            found = pos
        sentence_offsets.append(found)
        pos = found + len(s)
    sentence_offsets.append(len(text))  # sentinel

    def idx_for_offset(offset: int) -> int:
        for i in range(len(sentences)):
            if sentence_offsets[i] <= offset < sentence_offsets[i + 1]:
                return i
        return max(0, len(sentences) - 1)

    # Paragraph starts (always includes 0).
    para = {0}
    for offset, line in line_offsets:
        if line.get("starts_para"):
            para.add(idx_for_offset(offset))
    paragraph_starts = sorted(para)

    # Sections: prefer PDF outline; fall back to detected headings.
    sections: list[dict] = []
    if toc:
        # First sentence index on each page (first line we kept on that page).
        first_idx_on_page: dict[int, int] = {}
        for offset, line in line_offsets:
            pg = line["page"]
            if pg not in first_idx_on_page:
                first_idx_on_page[pg] = idx_for_offset(offset)

        for level, title, page1 in toc:
            page = max(0, page1 - 1)
            idx = None
            while page < total_pages:
                if page in first_idx_on_page:
                    idx = first_idx_on_page[page]
                    break
                page += 1
            if idx is not None:
                sections.append({"title": title.strip(), "idx": idx, "level": level})
    else:
        for offset, line in line_offsets:
            if line.get("is_heading"):
                sections.append({
                    "title": line["text"].strip().rstrip("."),
                    "idx": idx_for_offset(offset),
                    "level": 1,
                })

    # Deduplicate by idx, keeping first occurrence.
    seen: set[int] = set()
    deduped: list[dict] = []
    for s in sections:
        if s["idx"] not in seen:
            deduped.append(s)
            seen.add(s["idx"])
    sections = deduped

    return {
        "text": text,
        "sentences": sentences,
        "paragraph_starts": paragraph_starts,
        "sections": sections,
    }


_ABBREVS = {
    "e.g", "i.e", "cf", "etc", "al", "fig", "eq", "eqs", "ref", "refs",
    "vs", "approx", "no", "vol", "pp", "ch", "sec", "mr", "mrs", "dr", "prof",
}


def split_sentences(text: str) -> list[str]:
    raw = re.split(r"(?<=[.!?])\s+(?=[\"'(\[A-Z0-9])", text)
    out: list[str] = []
    for piece in raw:
        piece = piece.strip()
        if not piece:
            continue
        if out:
            prev = out[-1]
            m = re.search(r"(\w+)\.$", prev)
            if m and m.group(1).lower() in _ABBREVS:
                out[-1] = prev + " " + piece
                continue
        out.append(piece)
    return out


# ---------- Anthropic call ----------

@st.cache_resource
def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=API_KEY)


def ask_claude(
    paper_text: str,
    sentence: str,
    history: list[dict],
    question: str,
) -> str:
    client = get_client()
    system = [
        {"type": "text", "text": SYSTEM_PROMPT},
        {
            "type": "text",
            "text": f"<paper>\n{paper_text}\n</paper>",
            "cache_control": {"type": "ephemeral"},
        },
    ]
    messages: list[dict] = [
        {"role": turn["role"], "content": turn["content"]} for turn in history
    ]
    messages.append(
        {
            "role": "user",
            "content": (
                f"I'm on this sentence from the paper:\n\n\u201c{sentence}\u201d\n\n"
                f"My question: {question}"
            ),
        }
    )

    placeholder = st.empty()
    buf: list[str] = []
    with client.messages.stream(
        model=MODEL,
        max_tokens=4096,
        thinking={"type": "adaptive"},
        system=system,
        messages=messages,
    ) as stream:
        for text in stream.text_stream:
            buf.append(text)
            placeholder.markdown("".join(buf))
    return "".join(buf)


# ---------- Session state ----------

def _init_state() -> None:
    st.session_state.setdefault("study_path", None)
    st.session_state.setdefault("study_state", None)
    st.session_state.setdefault("paper_bytes", None)
    st.session_state.setdefault("paper_text", "")


def _load_study(path: Path) -> None:
    state, pdf_bytes = open_study(path)
    st.session_state.study_path = path
    st.session_state.study_state = state
    st.session_state.paper_bytes = pdf_bytes
    st.session_state.paper_text = " ".join(state.get("sentences", []))


def _close_study() -> None:
    st.session_state.study_path = None
    st.session_state.study_state = None
    st.session_state.paper_bytes = None
    st.session_state.paper_text = ""


def autosave() -> None:
    if st.session_state.study_path and st.session_state.study_state:
        save_study(
            st.session_state.study_path,
            st.session_state.study_state,
            st.session_state.paper_bytes,
        )


# ---------- UI: home ----------

def render_home() -> None:
    st.title(f"PEX — Paper Explainer  \u00b7  v{APP_VERSION}")
    st.caption(
        "Walk through a research paper one sentence at a time, with Claude on "
        "hand to clarify anything unclear."
    )

    if not API_KEY:
        st.warning("No API key found. Set `PEX_CLAUDE_API_KEY` in `~/.env`.")

    new_col, open_col = st.columns(2)

    with new_col:
        st.subheader("New Study")
        creating = st.session_state.get("creating", False)

        with st.form("new_study_form", clear_on_submit=False):
            pdf = st.file_uploader(
                "PDF", type=["pdf"], key="new_pdf", disabled=creating
            )
            name = st.text_input(
                "Name",
                key="new_name",
                placeholder="e.g. Attention Is All You Need",
                disabled=creating,
            )
            submitted = st.form_submit_button(
                "Create" if not creating else "Creating…",
                type="primary",
                disabled=creating,
            )

        # On submit: stash the payload and rerun so the form re-renders in a
        # disabled state before we start the slow parse.
        if submitted and not creating:
            if not pdf or not name.strip():
                st.error("Please provide both a PDF and a name.")
            else:
                st.session_state["_pending_study"] = {
                    "pdf_bytes": pdf.read(),
                    "pdf_name": pdf.name,
                    "name": name,
                }
                st.session_state["creating"] = True
                st.rerun()

        # Second pass: actually do the work while the form is disabled.
        if creating and "_pending_study" in st.session_state:
            payload = st.session_state["_pending_study"]
            try:
                with st.status("Creating Study…", expanded=True) as status:
                    pbar = st.progress(0.0, text="Extracting text from PDF…")

                    def _on_progress(frac: float) -> None:
                        pbar.progress(
                            min(frac, 1.0),
                            text=f"Extracting text from PDF… ({int(frac * 100)}%)",
                        )

                    extracted = extract_structured(
                        payload["pdf_bytes"], progress_callback=_on_progress
                    )
                    pbar.progress(1.0, text="Splitting into sentences…")
                    n_sent = len(extracted["sentences"])
                    n_sec = len(extracted["sections"])
                    n_para = len(extracted["paragraph_starts"])
                    pbar.progress(
                        1.0,
                        text=(
                            f"Found {n_sent} sentences, {n_para} paragraphs, "
                            f"{n_sec} sections. Saving Study…"
                        ),
                    )
                    path = create_study(
                        payload["name"],
                        payload["pdf_name"],
                        payload["pdf_bytes"],
                        extracted,
                    )
                    status.update(
                        label=f"Study ready: {path.stem}",
                        state="complete",
                        expanded=False,
                    )
                _load_study(path)
                st.session_state.pop("creating", None)
                st.session_state.pop("_pending_study", None)
                st.rerun()
            except FileExistsError as e:
                st.error(str(e))
                st.session_state.pop("creating", None)
                st.session_state.pop("_pending_study", None)
            except Exception as e:
                st.error(f"Could not create study: {e}")
                st.session_state.pop("creating", None)
                st.session_state.pop("_pending_study", None)

    with open_col:
        st.subheader("Open Study")
        studies = list_studies()
        if not studies:
            st.caption(f"No studies yet. They'll appear here once you create one.")
            st.caption(f"Location: `{STUDIES_DIR}`")
        else:
            confirming = st.session_state.get("confirm_delete")
            for entry in studies:
                path_key = str(entry["path"])
                mtime = datetime.fromtimestamp(entry["mtime"]).strftime("%Y-%m-%d %H:%M")
                open_col_row, del_col_row = st.columns([5, 1])
                with open_col_row:
                    if st.button(
                        f"**{entry['name']}**  \n_last opened {mtime}_",
                        key=f"open_{path_key}",
                        use_container_width=True,
                    ):
                        _load_study(entry["path"])
                        st.rerun()
                with del_col_row:
                    if st.button(
                        "🗑",
                        key=f"delete_{path_key}",
                        use_container_width=True,
                        help="Delete this Study",
                    ):
                        st.session_state["confirm_delete"] = path_key
                        st.rerun()
                if confirming == path_key:
                    cc1, cc2 = st.columns([1, 1])
                    with cc1:
                        if st.button(
                            f"Confirm delete",
                            key=f"confirm_{path_key}",
                            type="primary",
                            use_container_width=True,
                        ):
                            delete_study(entry["path"])
                            st.session_state.pop("confirm_delete", None)
                            st.rerun()
                    with cc2:
                        if st.button(
                            "Cancel",
                            key=f"cancel_{path_key}",
                            use_container_width=True,
                        ):
                            st.session_state.pop("confirm_delete", None)
                            st.rerun()


# ---------- UI: reading ----------

def render_reading() -> None:
    state = st.session_state.study_state
    sentences = state["sentences"]
    total = len(sentences)
    idx = state["idx"]

    # Keyboard shortcuts: dispatch before rendering so next/prev update the
    # displayed sentence in the same pass.
    kb = keyboard(key="kb")
    kb_nonce_key = "kb_nonce"
    if kb and kb.get("nonce") != st.session_state.get(kb_nonce_key):
        st.session_state[kb_nonce_key] = kb["nonce"]
        action = kb.get("type")
        paragraph_starts = state.get("paragraph_starts") or [0]
        if action == "next" and idx < total - 1:
            state["idx"] = idx + 1
            idx = state["idx"]
            autosave()
            st.rerun()
        elif action == "prev" and idx > 0:
            state["idx"] = idx - 1
            idx = state["idx"]
            autosave()
            st.rerun()
        elif action == "next_para":
            target = _next_para(paragraph_starts, idx)
            if target is not None:
                state["idx"] = target
                idx = target
                autosave()
                st.rerun()
        elif action == "prev_para":
            target = _prev_para(paragraph_starts, idx)
            if target is not None:
                state["idx"] = target
                idx = target
                autosave()
                st.rerun()
        elif action == "preset":
            scope = (kb.get("selection") or "").strip() or sentences[idx]
            st.session_state["pending_question"] = build_preset_question(
                kb.get("preset", ""), scope
            )

    with st.sidebar:
        st.title("PEX")
        st.caption(f"v{APP_VERSION}")
        if st.button("← Close Study", use_container_width=True):
            _close_study()
            st.rerun()
        st.markdown(f"**{st.session_state.study_path.stem}**")
        st.caption(f"Source: {state['paper_filename']}")
        st.markdown(f"Sentence **{idx + 1}** of **{total}**")
        st.progress((idx + 1) / total)

        # Search — jump to a sentence containing a word or phrase.
        query = st.text_input(
            "Search",
            key="search_query",
            placeholder="Find a word or phrase…",
        )
        if query:
            matches = _search_sentences(sentences, query)
            if not matches:
                st.caption("No matches.")
            else:
                shown = matches[:20]
                st.caption(f"{len(matches)} match(es){', showing first 20' if len(matches) > 20 else ''}")
                for m_idx, snippet in shown:
                    if st.button(
                        f"**{m_idx + 1}.** {snippet}",
                        key=f"srch_{m_idx}",
                        use_container_width=True,
                    ):
                        state["idx"] = m_idx
                        autosave()
                        st.rerun()

        # Section list — jump by structural outline.
        sections_list = state.get("sections") or []
        if sections_list:
            st.divider()
            st.caption("Sections")
            for s in sections_list:
                level = max(1, int(s.get("level", 1)))
                indent = "\u2003" * (level - 1)  # em-space per level
                s_idx = int(s["idx"])
                title = s.get("title") or f"Section @ sentence {s_idx + 1}"
                label = f"{indent}{title}"
                if st.button(label, key=f"sec_{s_idx}_{title[:20]}", use_container_width=True):
                    state["idx"] = s_idx
                    autosave()
                    st.rerun()

    left, right = st.columns([3, 2])

    with left:
        st.caption("Context")
        start = max(0, idx - CONTEXT_WINDOW)
        end = min(total, idx + CONTEXT_WINDOW + 1)

        def dim_block(indices) -> str:
            parts = []
            for i in indices:
                distance = abs(i - idx)
                opacity = max(0.25, 0.65 - 0.08 * distance)
                parts.append(
                    f"<div style='opacity:{opacity:.2f}; margin:6px 0; "
                    f"line-height:1.5'>{html.escape(sentences[i])}</div>"
                )
            return "".join(parts)

        if start < idx:
            st.markdown(dim_block(range(start, idx)), unsafe_allow_html=True)

        sentence_box(sentence=sentences[idx], key=f"sb_{idx}")

        if idx + 1 < end:
            st.markdown(dim_block(range(idx + 1, end)), unsafe_allow_html=True)

    with right:
        prev_col, next_col = st.columns(2)
        with prev_col:
            if st.button("← Previous", disabled=idx == 0, use_container_width=True):
                state["idx"] = idx - 1
                autosave()
                st.rerun()
        with next_col:
            if st.button("Next →", disabled=idx >= total - 1, use_container_width=True):
                state["idx"] = idx + 1
                autosave()
                st.rerun()

        paragraph_starts = state.get("paragraph_starts") or [0]
        prev_p = _prev_para(paragraph_starts, idx)
        next_p = _next_para(paragraph_starts, idx)
        pp_col, np_col = st.columns(2)
        with pp_col:
            if st.button(
                "⟵ Paragraph", disabled=prev_p is None, use_container_width=True
            ):
                state["idx"] = int(prev_p)
                autosave()
                st.rerun()
        with np_col:
            if st.button(
                "Paragraph ⟶", disabled=next_p is None, use_container_width=True
            ):
                state["idx"] = int(next_p)
                autosave()
                st.rerun()

        preset_result = preset_bar(key=f"pb_{idx}")
        nonce_key = f"pb_nonce_{idx}"
        if preset_result and preset_result.get("nonce") != st.session_state.get(nonce_key):
            st.session_state[nonce_key] = preset_result["nonce"]
            scope = (preset_result.get("selection") or "").strip() or sentences[idx]
            st.session_state["pending_question"] = build_preset_question(
                preset_result.get("preset", ""), scope
            )

        st.caption("Ask about this sentence")
        history = state["qa_by_idx"].setdefault(idx, [])

        for turn in history:
            with st.chat_message(turn["role"]):
                st.markdown(turn["content"])

        typed = st.chat_input("What's unclear?")
        question = st.session_state.pop("pending_question", None) or typed
        if question:
            with st.chat_message("user"):
                st.markdown(question)
            with st.chat_message("assistant"):
                answer = ask_claude(
                    st.session_state.paper_text,
                    sentences[idx],
                    history,
                    question,
                )
            history.append({"role": "user", "content": question})
            history.append({"role": "assistant", "content": answer})
            autosave()


# ---------- Entry point ----------

st.set_page_config(page_title=f"PEX v{APP_VERSION} — Paper Explainer", layout="wide")
_init_state()

if st.session_state.study_path is None:
    render_home()
else:
    render_reading()
