"""Custom Streamlit components for PEX.

- `sentence_box`  — displays the active sentence in a bordered box with a
  scope indicator and hint. Tracks the user's text selection and writes it
  to `localStorage["pex_selection"]` so a separate iframe can read it.
- `preset_bar`    — renders the four preset buttons. On click, reads
  `localStorage["pex_selection"]` and returns `{preset, selection, nonce}`.

Both iframes are served from the same origin (the Streamlit server), so they
share `localStorage`.
"""

from __future__ import annotations

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


def sentence_box(sentence: str, key: Optional[str] = None) -> None:
    _sentence_box(sentence=sentence, key=key, default=None)


def preset_bar(key: Optional[str] = None) -> Optional[dict]:
    return _preset_bar(key=key, default=None)
