"""PEX — Paper Explainer.

Streamlit app that walks you through a research paper one sentence at a time,
with Claude available to clarify words, phrases, or ideas on demand.
"""

from __future__ import annotations

import html
import os
import re
from datetime import datetime
from pathlib import Path

CONTEXT_WINDOW = 5

import anthropic
import fitz  # pymupdf
import streamlit as st
from dotenv import load_dotenv

from components import preset_bar, sentence_box
from study import (
    STUDIES_DIR,
    create_study,
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

def extract_text(pdf_bytes: bytes) -> str:
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        pages = [page.get_text("text") for page in doc]
    text = "\n".join(pages)
    text = re.sub(r"-\n(\w)", r"\1", text)
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


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
    st.session_state.paper_text = extract_text(pdf_bytes)


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
    st.title("PEX — Paper Explainer")
    st.caption(
        "Walk through a research paper one sentence at a time, with Claude on "
        "hand to clarify anything unclear."
    )

    if not API_KEY:
        st.warning("No API key found. Set `PEX_CLAUDE_API_KEY` in `~/.env`.")

    new_col, open_col = st.columns(2)

    with new_col:
        st.subheader("New Study")
        pdf = st.file_uploader("PDF", type=["pdf"], key="new_pdf")
        name = st.text_input("Name", key="new_name", placeholder="e.g. Attention Is All You Need")
        if st.button("Create", type="primary", disabled=not (pdf and name)):
            try:
                pdf_bytes = pdf.read()
                text = extract_text(pdf_bytes)
                sentences = split_sentences(text)
                path = create_study(name, pdf.name, pdf_bytes, sentences)
                _load_study(path)
                st.rerun()
            except FileExistsError as e:
                st.error(str(e))
            except Exception as e:
                st.error(f"Could not create study: {e}")

    with open_col:
        st.subheader("Open Study")
        studies = list_studies()
        if not studies:
            st.caption(f"No studies yet. They'll appear here once you create one.")
            st.caption(f"Location: `{STUDIES_DIR}`")
        else:
            for entry in studies:
                mtime = datetime.fromtimestamp(entry["mtime"]).strftime("%Y-%m-%d %H:%M")
                if st.button(
                    f"**{entry['name']}**  \n_last opened {mtime}_",
                    key=f"open_{entry['path']}",
                    use_container_width=True,
                ):
                    _load_study(entry["path"])
                    st.rerun()


# ---------- UI: reading ----------

def render_reading() -> None:
    state = st.session_state.study_state
    sentences = state["sentences"]
    total = len(sentences)
    idx = state["idx"]

    with st.sidebar:
        st.title("PEX")
        if st.button("← Close Study", use_container_width=True):
            _close_study()
            st.rerun()
        st.markdown(f"**{st.session_state.study_path.stem}**")
        st.caption(f"Source: {state['paper_filename']}")
        st.markdown(f"Sentence **{idx + 1}** of **{total}**")
        st.progress((idx + 1) / total)
        jump = st.number_input(
            "Jump to sentence",
            min_value=1,
            max_value=total,
            value=idx + 1,
            step=1,
        )
        if jump - 1 != idx:
            state["idx"] = jump - 1
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

st.set_page_config(page_title="PEX — Paper Explainer", layout="wide")
_init_state()

if st.session_state.study_path is None:
    render_home()
else:
    render_reading()
