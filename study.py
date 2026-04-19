"""Study file I/O for PEX.

A Study is a `.pex` file — a ZIP archive containing `paper.pdf` (the source
document) and `state.json` (progress, Q&A history, metadata). Studies live in
`~/PEX_Studies/` by default.
"""

from __future__ import annotations

import json
import re
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

STUDIES_DIR = Path.home() / "PEX_Studies"
STATE_VERSION = 3
STATE_FILE = "state.json"
PDF_FILE = "paper.pdf"


def ensure_studies_dir() -> Path:
    STUDIES_DIR.mkdir(parents=True, exist_ok=True)
    return STUDIES_DIR


def safe_name(name: str) -> str:
    """Turn a user-supplied study name into a safe filename stem."""
    cleaned = re.sub(r"[^A-Za-z0-9 _\-]+", "_", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "untitled"


def list_studies() -> list[dict]:
    """Return existing .pex files under STUDIES_DIR, newest first."""
    ensure_studies_dir()
    entries = []
    for path in STUDIES_DIR.glob("*.pex"):
        stat = path.stat()
        entries.append({"path": path, "name": path.stem, "mtime": stat.st_mtime})
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


def create_study(
    name: str,
    paper_filename: str,
    pdf_bytes: bytes,
    extracted: dict,
) -> Path:
    """Create a new .pex file. Raises FileExistsError if the name is taken.

    `extracted` is the dict returned by `extract_structured` — its
    `sentences`, `paragraph_starts`, and `sections` are persisted into
    `state.json`.
    """
    ensure_studies_dir()
    path = STUDIES_DIR / f"{safe_name(name)}.pex"
    if path.exists():
        raise FileExistsError(f"A study named '{path.stem}' already exists.")
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "version": STATE_VERSION,
        "paper_filename": paper_filename,
        "created_at": now,
        "updated_at": now,
        "sentences": extracted["sentences"],
        "paragraph_starts": extracted.get("paragraph_starts", [0]),
        "sections": extracted.get("sections", []),
        "sentence_pages": extracted.get("sentence_pages", []),
        "idx": 0,
        "qa_by_idx": {},
    }
    _write(path, state, pdf_bytes)
    return path


def open_study(path: Path) -> tuple[dict, bytes]:
    """Return (state, pdf_bytes) from a .pex file."""
    with zipfile.ZipFile(path, "r") as zf:
        with zf.open(STATE_FILE) as f:
            state = json.load(f)
        with zf.open(PDF_FILE) as f:
            pdf_bytes = f.read()
    # qa_by_idx is serialized with string keys; convert back to int
    state["qa_by_idx"] = {int(k): v for k, v in state.get("qa_by_idx", {}).items()}
    # Backward-compat defaults for older studies.
    state.setdefault("paragraph_starts", [0])
    state.setdefault("sections", [])
    state.setdefault("sentence_pages", [])
    return state, pdf_bytes


def save_study(path: Path, state: dict, pdf_bytes: bytes) -> None:
    """Overwrite the .pex file with updated state. Bumps updated_at."""
    state = dict(state)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write(path, state, pdf_bytes)


def delete_study(path: Path) -> None:
    """Remove a .pex file. Silent no-op if it's already gone."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _write(path: Path, state: dict, pdf_bytes: bytes) -> None:
    # Atomic write: build in a unique temp file then rename into place so a
    # crash mid-save can't leave a half-written .pex on disk. The uuid suffix
    # prevents concurrent saves (from rapid reruns) from colliding on the
    # same temp filename.
    tmp = path.with_suffix(path.suffix + f".tmp.{uuid.uuid4().hex}")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(STATE_FILE, json.dumps(state, indent=2, ensure_ascii=False))
            zf.writestr(PDF_FILE, pdf_bytes)
        tmp.replace(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
