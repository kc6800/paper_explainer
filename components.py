"""Custom Streamlit components for PEX.

- `sentence_box`  — displays the active sentence in a bordered box with a
  scope indicator and hint. Tracks the user's text selection and writes it
  to `localStorage["pex_selection"]` so a separate iframe can read it.
- `preset_bar`    — renders the four preset buttons. On click, reads
  `localStorage["pex_selection"]` and returns `{preset, selection, nonce}`.
- `keyboard`      — zero-height iframe capturing global keyboard shortcuts.
- `pdf_viewer`    — fixed-height scrollable pane showing the current PDF
  page; auto-scrolls its own scroll container to the highlighted region.

All iframes are served from the same origin (the Streamlit server), so they
share `localStorage`.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional

import streamlit.components.v1 as components

_FRONTEND = Path(__file__).parent / "frontend"

_sentence_box = components.declare_component(
    "pex_sentence_box", path=str(_FRONTEND / "sentence_box")
)
_preset_bar = components.declare_component(
    "pex_preset_bar", path=str(_FRONTEND / "preset_bar")
)
_keyboard = components.declare_component(
    "pex_keyboard", path=str(_FRONTEND / "keyboard")
)
_pdf_viewer = components.declare_component(
    "pex_pdf_viewer", path=str(_FRONTEND / "pdf_viewer")
)


def sentence_box(sentence: str, key: Optional[str] = None) -> None:
    _sentence_box(sentence=sentence, key=key, default=None)


def preset_bar(key: Optional[str] = None) -> Optional[dict]:
    return _preset_bar(key=key, default=None)


def keyboard(key: Optional[str] = None) -> Optional[dict]:
    return _keyboard(key=key, default=None)


def pdf_viewer(
    png_bytes: bytes,
    highlight_offset_px: float,
    key: Optional[str] = None,
) -> None:
    """Render a scrollable PDF-page pane that auto-scrolls to a highlight.

    `png_bytes` is the rendered page (with highlight rectangles already
    composited on top). `highlight_offset_px` is the y-coordinate of the
    topmost highlight in the native pixels of the rendered image.
    """
    b64 = base64.b64encode(png_bytes).decode("ascii")
    _pdf_viewer(
        image_b64=b64,
        highlight_offset_px=float(highlight_offset_px),
        key=key,
        default=None,
    )
