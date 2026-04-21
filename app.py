"""PEX — Paper Explainer.

Streamlit app that walks you through a research paper one sentence at a time,
with Claude available to clarify words, phrases, or ideas on demand.
"""

from __future__ import annotations

import io
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

APP_VERSION = "1.1"

import anthropic
import fitz  # pymupdf
import streamlit as st
from dotenv import load_dotenv
from pydantic import BaseModel

from components import keyboard, pdf_viewer, preset_bar, sentence_box
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


PDF_VIEWER_WIDTH = 720  # px — fixed output width of the rendered page image
_PAGE_CACHE_KEY = "_pex_page_cache"
_PAGE_CACHE_MAX = 10


def _render_page_base(pdf_bytes: bytes, page_num: int, width_px: int):
    """Render a PDF page to a PIL image at the given target width.

    Returns (image, zoom). `image` is None if the page index is out of range.
    """
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        if not (0 <= page_num < len(doc)):
            return None, 1.0
        page = doc[page_num]
        zoom = width_px / page.rect.width
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGBA")
        return img, zoom


def _get_cached_page(study_key: str, pdf_bytes: bytes, page_num: int, width_px: int):
    """Cache the base (un-highlighted) render per page with a small LRU bound."""
    cache = st.session_state.setdefault(_PAGE_CACHE_KEY, {})
    key = (study_key, page_num, width_px)
    if key in cache:
        # Bump to most-recent by re-inserting (dict preserves insertion order).
        entry = cache.pop(key)
        cache[key] = entry
        return entry
    base, zoom = _render_page_base(pdf_bytes, page_num, width_px)
    if len(cache) >= _PAGE_CACHE_MAX:
        cache.pop(next(iter(cache)))
    cache[key] = (base, zoom)
    return base, zoom


def render_sentence_page(
    pdf_bytes: bytes,
    study_key: str,
    page_num: int,
    rects: list,
    width_px: int = PDF_VIEWER_WIDTH,
) -> tuple[bytes | None, float]:
    """Render the PDF page and highlight `rects`.

    Returns `(png_bytes, highlight_offset_px)` where `highlight_offset_px`
    is the y-coordinate of the topmost highlight in the rendered image's
    native pixels. Callers use this to scroll the parent page to bring the
    highlight into view. Returns `(None, 0.0)` if the page is invalid.
    """
    base, zoom = _get_cached_page(study_key, pdf_bytes, page_num, width_px)
    if base is None:
        return None, 0.0
    img = base.copy()
    top_y: float | None = None
    if rects:
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        for rect in rects:
            x0, y0, x1, y1 = rect
            draw.rectangle(
                [x0 * zoom, y0 * zoom, x1 * zoom, y1 * zoom],
                fill=(255, 230, 90, 110),
            )
            if top_y is None or y0 < top_y:
                top_y = y0
        img = Image.alpha_composite(img, overlay)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    highlight_offset_px = (top_y or 0.0) * zoom
    return buf.getvalue(), highlight_offset_px


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
                    x0, y0, x1, y1 = line["bbox"]
                    lines.append({
                        "text": text,
                        "size": avg_size,
                        "y0": y0,
                        "y1": y1,
                        "bbox": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)],
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

        # Pass 3 — join and record per-line char offsets + piece length.
        pieces: list[str] = []
        line_offsets: list[tuple[int, int, dict]] = []  # (offset, piece_len, line)
        for line in flat_lines:
            if pieces:
                pieces.append(" ")
            offset = sum(len(p) for p in pieces)
            txt = line["text"]
            # Append a period to un-terminated headings so the sentence
            # splitter treats them as their own sentence. Gate this on the
            # heading having at least 2 words — otherwise an emphasized
            # acronym in body text (e.g. "SGB") gets an unwanted period
            # and breaks the following sentence.
            if (
                line.get("is_heading")
                and len(line["text"].split()) >= 2
                and not txt.rstrip().endswith((".", "!", "?", ":"))
            ):
                txt = txt + "."
            pieces.append(txt)
            line_offsets.append((offset, len(txt), line))

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
    for offset, _piece_len, line in line_offsets:
        if line.get("starts_para"):
            para.add(idx_for_offset(offset))
    paragraph_starts = sorted(para)

    # Per-sentence page + highlight rects (on the first page the sentence
    # touches). A sentence may span multiple lines on that page — we
    # collect all overlapping line bboxes as rects.
    sentence_pages: list[dict] = []
    for i in range(len(sentences)):
        s_start, s_end = sentence_offsets[i], sentence_offsets[i + 1]
        by_page: dict[int, list[list[float]]] = {}
        for offset, piece_len, line in line_offsets:
            if offset < s_end and (offset + piece_len) > s_start:
                by_page.setdefault(line["page"], []).append(line["bbox"])
        if by_page:
            first = min(by_page)
            sentence_pages.append({"page": first, "rects": by_page[first]})
        else:
            sentence_pages.append({"page": 0, "rects": []})

    # Sections: prefer the PDF's embedded outline. If it's empty, leave
    # `sections` blank here — the caller (Study creation) decides whether
    # to fill it in via a semantic (LLM) fallback.
    sections: list[dict] = []
    if toc:
        # First sentence index on each page (first line we kept on that page).
        first_idx_on_page: dict[int, int] = {}
        for offset, _piece_len, line in line_offsets:
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
        "sentence_pages": sentence_pages,
    }


_ABBREVS = (
    "e.g", "i.e", "cf", "etc", "et al", "al", "fig", "eq", "eqs",
    "ref", "refs", "vs", "approx", "no", "vol", "p", "pp", "ch", "sec",
    "mr", "mrs", "dr", "prof",
)

# Matches a known abbreviation at the tail of the previous chunk. The
# leading `(?:^|[\s(])` anchors the abbreviation as its own word so it
# doesn't spuriously match inside another word.
_ABBREV_TAIL = re.compile(
    r"(?:^|[\s(])(?:" + "|".join(re.escape(a) for a in _ABBREVS) + r")\.$",
    re.IGNORECASE,
)

# Matches an "initial" — a single uppercase letter followed by a period at
# the tail of the previous chunk (e.g. the `J.` in `J. Smith`).
_INITIAL_TAIL = re.compile(r"(?:^|\s)[A-Z]\.$")

# A bracketed/parenthesized reference followed by a lowercase word is a
# citation continuation, not a new sentence — e.g. `SGB. [20] uses ...` or
# `Smith. (2019) shows ...`.
_CITATION_CONT = re.compile(r"^[\[(][^\]\)]*[\])]\s+[a-z]")


def split_sentences(text: str) -> list[str]:
    raw = re.split(r"(?<=[.!?])\s+(?=[\"'(\[A-Z0-9])", text)
    out: list[str] = []
    for piece in raw:
        piece = piece.strip()
        if not piece:
            continue
        if out:
            prev = out[-1]
            if (
                _ABBREV_TAIL.search(prev)
                or _INITIAL_TAIL.search(prev)
                or _CITATION_CONT.match(piece)
            ):
                out[-1] = prev + " " + piece
                continue
        out.append(piece)
    return out


# ---------- Anthropic call ----------

@st.cache_resource
def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=API_KEY)


class _LLMSection(BaseModel):
    title: str
    idx: int
    level: int


class _LLMSectionList(BaseModel):
    sections: list[_LLMSection]


def extract_sections_via_llm(sentences: list[str]) -> list[dict]:
    """Ask Claude to infer a section outline when the PDF has no embedded TOC.

    Returns a list of `{title, idx, level}` dicts. Degrades to `[]` if there's
    no API key or the call fails — the feature is opportunistic, not
    essential for Study creation.
    """
    if not API_KEY or not sentences:
        return []
    numbered = "\n".join(f"[{i}] {s}" for i, s in enumerate(sentences))
    prompt = (
        "You are structuring a research paper into a table of contents.\n\n"
        "Below are the paper's sentences, numbered from 0. Identify the "
        "section and subsection boundaries that reflect the paper's natural "
        "structure (e.g., Abstract, Introduction, Related Work, Method, "
        "Experiments, Results, Discussion, Conclusion, References, "
        "Acknowledgements). Prefer the section titles as they literally "
        "appear in the text when you can spot them.\n\n"
        "For each section return:\n"
        "  - title: a concise heading as it would appear in a table of "
        "contents\n"
        "  - idx: the sentence index where the section begins\n"
        "  - level: 1 for top-level, 2 for subsection, 3 for sub-subsection\n\n"
        "Be selective — typical research papers have 5–20 top-level sections. "
        "Do not include every paragraph. Output sections sorted by `idx`.\n\n"
        f"Sentences:\n{numbered}"
    )
    try:
        response = get_client().messages.parse(
            model=MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
            output_format=_LLMSectionList,
        )
    except Exception:
        return []
    result = getattr(response, "parsed_output", None)
    if result is None:
        return []
    total = len(sentences)
    cleaned: list[dict] = []
    seen: set[int] = set()
    for s in sorted(result.sections, key=lambda s: s.idx):
        idx = int(s.idx)
        if not (0 <= idx < total) or idx in seen:
            continue
        title = s.title.strip()
        if not title:
            continue
        cleaned.append({
            "title": title,
            "idx": idx,
            "level": max(1, min(3, int(s.level))),
        })
        seen.add(idx)
    return cleaned


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
                    # If the PDF had no embedded table of contents, fall back
                    # to a semantic (LLM) section inference. The font-size
                    # heuristic used previously missed a lot of papers.
                    if not extracted["sections"]:
                        pbar.progress(
                            1.0,
                            text=(
                                "No embedded outline — detecting sections "
                                "with Claude…"
                            ),
                        )
                        extracted["sections"] = extract_sections_via_llm(
                            extracted["sentences"]
                        )
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
        pct = int(round(100 * (idx + 1) / total))
        st.markdown(f"Sentence **{idx + 1}** of **{total}** ({pct}%)")
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
        sentence_box(sentence=sentences[idx], key=f"sb_{idx}")

        # Source PDF viewer — shows the page containing the active sentence
        # with the sentence's line rects highlighted.
        sent_pages = state.get("sentence_pages") or []
        loc = sent_pages[idx] if idx < len(sent_pages) else None
        if loc and st.session_state.paper_bytes:
            page_num = int(loc.get("page", 0))
            rects = loc.get("rects", []) or []
            img_bytes, highlight_offset_px = render_sentence_page(
                st.session_state.paper_bytes,
                str(st.session_state.study_path),
                page_num,
                rects,
            )
            if img_bytes:
                st.caption(f"Page {page_num + 1}")
                # The viewer is a fixed-height scrollable pane whose own
                # scroll is updated to bring the highlight into view on
                # every render. Keeps the pane anchored on the Streamlit
                # page regardless of where the highlight sits in the PDF.
                pdf_viewer(img_bytes, highlight_offset_px, key="pdf_viewer")
        else:
            st.caption(
                "PDF viewer not available for this Study — re-create it from the "
                "source PDF to enable page highlights."
            )

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
            # Auto-scroll the parent page so the newest assistant message is
            # brought into view. The script runs inside a zero-height iframe;
            # querySelectorAll on window.parent.document reaches the main app.
            st.components.v1.html(
                """
                <script>
                  requestAnimationFrame(() => {
                    const msgs = window.parent.document
                      .querySelectorAll('[data-testid="stChatMessage"]');
                    if (msgs.length) {
                      msgs[msgs.length - 1].scrollIntoView({
                        behavior: "smooth",
                        block: "start",
                      });
                    }
                  });
                </script>
                """,
                height=0,
            )


# ---------- Entry point ----------

st.set_page_config(page_title=f"PEX v{APP_VERSION} — Paper Explainer", layout="wide")
_init_state()

if st.session_state.study_path is None:
    render_home()
else:
    render_reading()
