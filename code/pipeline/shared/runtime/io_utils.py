from __future__ import annotations

import csv
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Iterable

from shared.artifact_metadata import (
    get_csv_artifact_doc,
    render_csv_comment_block,
)

_csv_field_limit = sys.maxsize
while True:
    try:
        csv.field_size_limit(_csv_field_limit)
        break
    except OverflowError:
        _csv_field_limit = int(_csv_field_limit / 10)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_csv(
    path: Path, rows: Iterable[dict[str, object]], fieldnames: list[str]
) -> None:
    ensure_parent(path)
    doc = get_csv_artifact_doc(path)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        if doc is not None:
            for line in render_csv_comment_block(doc):
                handle.write(f"# {line}\n")
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(_iter_data_lines(handle)))


def write_text(path: Path, text: str) -> None:
    ensure_parent(path)
    path.write_text(text, encoding="utf-8")


def _iter_data_lines(handle) -> Iterator[str]:
    for line in handle:
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        yield line
        break
    for line in handle:
        yield line
