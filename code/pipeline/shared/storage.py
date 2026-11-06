from __future__ import annotations

import csv
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Iterable

from shared.artifact_metadata import get_csv_artifact_doc, render_csv_comment_block

_csv_field_limit = sys.maxsize
while True:
    try:
        csv.field_size_limit(_csv_field_limit)
        break
    except OverflowError:
        _csv_field_limit = int(_csv_field_limit / 10)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def replace_with_retry(source: Path, target: Path) -> None:
    """Atomically commit, tolerating short Windows sharing/access conflicts only."""
    deadline = time.monotonic() + 3.0
    delay = 0.05
    for attempt in range(7):
        try:
            source.replace(target)
            return
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in {5, 32, 33}:
                raise
            remaining = deadline - time.monotonic()
            if attempt == 6 or remaining <= 0:
                raise
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 1.0)


def atomic_write_text(path: Path, text: str, *, encoding="utf-8") -> None:
    ensure_parent(path)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding=encoding, delete=False,
            dir=path.parent, prefix=path.name + ".", suffix=".tmp",
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def write_csv(
    path: Path, rows: Iterable[dict[str, object]], fieldnames: list[str]
) -> None:
    ensure_parent(path)
    doc = get_csv_artifact_doc(path)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8-sig", newline="", delete=False,
            dir=path.parent, prefix=path.name + ".", suffix=".tmp",
        ) as handle:
            temp_path = Path(handle.name)
            if doc is not None:
                for line in render_csv_comment_block(doc):
                    handle.write(f"# {line}\n")
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


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
