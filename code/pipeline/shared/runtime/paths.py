"""Resolve installed resources separately from caller-owned data/output paths."""

import os
from pathlib import Path


def io_path(value):
    """Absolute Windows extended path for I/O, without changing logical identity."""
    path = Path(value).absolute()
    if os.name != "nt" or str(path).startswith("\\\\?\\"):
        return path
    text = str(path)
    return Path(
        "\\\\?\\UNC\\" + text[2:] if text.startswith("\\\\") else "\\\\?\\" + text
    )


def project_root():
    return Path(__file__).resolve().parents[2]


