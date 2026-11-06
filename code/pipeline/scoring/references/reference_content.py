"""Load the current Stage 9 reference text; no external manifest fallback."""

from __future__ import annotations

import json
from pathlib import Path


def doi_to_dir_stem(doi: str) -> str:
    return doi.strip().lower().replace("/", "_")


def load_reference_fulltext(reference_root: Path, doi: str) -> str:
    """Return concatenated locally extracted reference text.

    Returns empty string if the reference cannot be found or read.
    """
    stem = doi_to_dir_stem(doi)
    ref_dir = reference_root / stem

    if not ref_dir.exists():
        return ""

    # Layout A: {doi_stem}/auto/{doi_stem}_content_list.json
    cl_path_a = ref_dir / "auto" / f"{stem}_content_list.json"
    if cl_path_a.exists():
        return _read_content_list(cl_path_a)

    return ""


def _read_content_list(path: Path) -> str:
    try:
        blocks = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""

    if not isinstance(blocks, list):
        return ""

    chunks: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        text = str(block.get("text", "")).strip()
        if text:
            chunks.append(text)

    return "\n\n".join(chunks)

