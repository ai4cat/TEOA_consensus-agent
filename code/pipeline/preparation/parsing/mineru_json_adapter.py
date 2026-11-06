from __future__ import annotations

from preparation.parsing.page_index import _parse_page_idx

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shared.pdf_utils import PageText
from shared.pdf_utils import ParsedPdf
from shared.text_cleaning import looks_like_reference_entry

_SECTION_PREFIX_RE = re.compile(r"^\d+(?:\.\d+)*\.?\s+\S")
_COMMON_SECTION_RE = re.compile(
    r"^(abstract|introduction|background|results(?:\s+and\s+discussion)?|discussion|conclusions?|"
    r"experimental(?:\s+section)?|materials(?:\s+and\s+methods)?|methods?|methodology|references)\b",
    re.IGNORECASE,
)
_POST_REFERENCE_HEADING_RE = re.compile(
    r"^(acknowledg(?:e)?ments?|author contributions?|competing interests?|additional information|"
    r"supplementary information|correspondence|peer review information|reprints and permissions|"
    r"publisher'?s note|open access)\b",
    re.IGNORECASE,
)
_REFERENCE_SPLIT_RE = re.compile(
    r"(?=(?:^|\s)(?:\d{1,3}[.)]|\[\d{1,3}\])\s+[A-Z][A-Za-z'鈥?-]{1,40}(?:,|\s+et\s+al\.))"
)
_TRANSITION_TAIL_RE = re.compile(
    r"(?:initially|however|therefore|thus|subsequently|finally|meanwhile|first(?:ly)?|second(?:ly)?|third(?:ly)?|next)\s*[,:;]?$",
    re.IGNORECASE,
)
_CHEMICAL_TOKEN_RE = re.compile(r"^(?:[A-Z][a-z]?\d*){1,5}[\w\-./]*$")


@dataclass(slots=True)
class MineruJsonEntry:
    text: str
    page_number: int
    section_title: str
    chunk_type: str
    source_block_type: str
    source_block_id: str
    source_bboxes: list[list[float]]
    source_heading_level: str = ""
    source_identity_json: str = ""


def parse_mineru_json_file(path: Path) -> ParsedPdf:
    entries = load_mineru_json_entries(path)
    pages: dict[int, list[str]] = {}
    for entry in entries:
        pages.setdefault(entry.page_number, []).append(entry.text)
    return ParsedPdf(
        pages=[
            PageText(page_number=page_number, text="\n\n".join(texts))
            for page_number, texts in sorted(pages.items())
        ]
    )


def _validate_persistent_blocks(blocks, seen):
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("source_block_id_kind") == "persistent":
            identifier = str(block.get("source_block_id", "")).strip()
            if not identifier or identifier in seen:
                raise ValueError(
                    "Missing or duplicate declared persistent source_block_id"
                )
            seen.add(identifier)
        if block.get("type") == "list" and isinstance(block.get("blocks"), list):
            _validate_persistent_blocks(block["blocks"], seen)


def load_mineru_json_entries(path: Path) -> list[MineruJsonEntry]:
    from preparation.parsing.content_list import normalize_content_list

    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    is_v2 = isinstance(payload, list) and any(
        isinstance(page, list) for page in payload
    )
    payload = normalize_content_list(payload)
    if isinstance(payload, list):
        _validate_persistent_blocks(payload, set())
        return _load_precise_content_list_entries(payload, preserve_page_indices=is_v2)
    if not isinstance(payload, dict):
        raise ValueError("MinerU JSON payload must be an object or a content list")
    seen = set()
    for page in payload.get("pdf_info", []):
        blocks = page.get("preproc_blocks") or page.get("para_blocks") or []
        if isinstance(blocks, list):
            _validate_persistent_blocks(blocks, seen)
    entries: list[MineruJsonEntry] = []
    current_section_title = ""
    current_section_heading_level = ""
    reference_section_active = False

    for page_number, page in enumerate(payload.get("pdf_info", []), start=1):
        # Older MinerU produced "preproc_blocks"; newer versions (vlm backend)
        # produce "para_blocks" with the same span/content schema. Accept both.
        raw_blocks = page.get("preproc_blocks") or page.get("para_blocks") or []
        if not isinstance(raw_blocks, list):
            continue
        # VLM backend wraps grouped text (numbered/bullet lists, reference lists)
        # in a {"type":"list","blocks":[...]} container. Flatten one level so
        # each inner text/title block is visible to the later classifier.
        blocks: list[dict[str, Any]] = []
        for b in raw_blocks:
            if not isinstance(b, dict):
                continue
            if str(b.get("type", "")).strip().lower() == "list":
                inner = b.get("blocks") or []
                if isinstance(inner, list):
                    blocks.extend(x for x in inner if isinstance(x, dict))
                continue
            blocks.append(b)
        for block in sorted(blocks, key=_block_sort_key):
            block_type = str(block.get("type", "")).strip().lower()
            if block_type in {"image", "table", "interline_equation", "discarded"}:
                continue
            # VLM backend tags reference/author-info lines as "ref_text" rather
            # than plain "text". Treat them as text for classification; the
            # existing logic below already decides body vs reference via heading
            # context and looks_like_reference_entry().
            if block_type == "ref_text":
                block_type = "text"
            if block_type not in {"text", "title"}:
                continue

            text = _extract_block_text(block)
            if not text:
                continue

            if block_type == "title":
                normalized_heading = _normalize_heading_text(text)
                if _is_reference_heading(normalized_heading):
                    reference_section_active = True
                    current_section_title = "References"
                    current_section_heading_level = "title"
                    continue
                if reference_section_active and _is_post_reference_heading(
                    normalized_heading
                ):
                    reference_section_active = False
                    current_section_title = text
                    current_section_heading_level = "title"
                    continue
                if reference_section_active:
                    continue
                if _looks_like_section_heading(text):
                    current_section_title = text
                    current_section_heading_level = "title"
                continue

            if reference_section_active:
                entries.extend(
                    _split_reference_entry(
                        _entry_from_block(
                            block=block,
                            page_number=page_number,
                            text=text,
                            section_title="References",
                            chunk_type="reference_entry",
                            source_heading_level=current_section_heading_level,
                        )
                    )
                )
                continue

            if _is_post_reference_heading(
                _normalize_heading_text(current_section_title)
            ):
                continue

            if looks_like_reference_entry(text):
                entries.extend(
                    _split_reference_entry(
                        _entry_from_block(
                            block=block,
                            page_number=page_number,
                            text=text,
                            section_title="References",
                            chunk_type="reference_entry",
                            source_heading_level=current_section_heading_level,
                        )
                    )
                )
                continue

            entry = _entry_from_block(
                block=block,
                page_number=page_number,
                text=text,
                section_title=current_section_title,
                chunk_type="body_paragraph",
                source_heading_level=current_section_heading_level,
            )
            if entries and _should_merge_entries(entries[-1], entry):
                entries[-1] = _merge_entries(entries[-1], entry)
                continue
            entries.append(entry)

    return entries


def _load_precise_content_list_entries(
    content_list: list[object],
    *,
    preserve_page_indices: bool = False,
) -> list[MineruJsonEntry]:
    entries: list[MineruJsonEntry] = []
    current_section_title = ""
    current_section_heading_level = ""
    reference_section_active = False

    page_numbers: list[int] = []
    for block in content_list:
        if not isinstance(block, dict):
            continue
        page_idx = _parse_page_idx(block.get("page_idx"))
        if page_idx is None:
            page_idx = _parse_page_idx(block.get("page_number"))
        if page_idx is not None and page_idx >= 0:
            page_numbers.append(page_idx)
    page_number_offset = (
        0 if preserve_page_indices else min(page_numbers) if page_numbers else 0
    )

    for index, block in enumerate(content_list):
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type", "")).strip().lower()
        if block_type in {"image", "table", "discarded"}:
            continue

        page_idx = _parse_page_idx(block.get("page_idx"))
        if page_idx is None:
            page_idx = _parse_page_idx(block.get("page_number"))
        if page_idx is None or page_idx < 0:
            continue
        page_number = page_idx - page_number_offset + 1

        if block_type == "page_number" or block_type == "header":
            continue

        if block_type == "text":
            text = _normalize_mineru_text(str(block.get("text", "")))
            if not text:
                continue
            text_level = block.get("text_level")
            if (
                text_level in {1, "1"}
                if block.get("parser") == "pymupdf"
                else _looks_like_precise_heading(text, text_level)
            ):
                normalized_heading = _normalize_heading_text(text)
                if _is_reference_heading(normalized_heading):
                    reference_section_active = True
                    current_section_title = "References"
                    current_section_heading_level = str(text_level or "")
                    continue
                if reference_section_active and _is_post_reference_heading(
                    normalized_heading
                ):
                    reference_section_active = False
                    current_section_title = text
                    current_section_heading_level = str(text_level or "")
                    continue
                if reference_section_active:
                    continue
                current_section_title = text
                current_section_heading_level = str(text_level or "")
                continue
            if reference_section_active:
                entry = _entry_from_block(
                    block={**block, "index": block.get("index", index)},
                    page_number=page_number,
                    text=text,
                    section_title="References",
                    chunk_type="reference_entry",
                    source_heading_level=current_section_heading_level,
                )
                entries.extend(_split_reference_entry(entry))
                continue
            if _is_post_reference_heading(
                _normalize_heading_text(current_section_title)
            ):
                continue
            if looks_like_reference_entry(text):
                entry = _entry_from_block(
                    block={**block, "index": block.get("index", index)},
                    page_number=page_number,
                    text=text,
                    section_title="References",
                    chunk_type="reference_entry",
                    source_heading_level=current_section_heading_level,
                )
                entries.extend(_split_reference_entry(entry))
                continue
            is_caption = block.get("kind") == "caption_candidate" or (
                block.get("parser") == "pymupdf"
                and bool(re.match(r"^(?:Fig(?:ure)?\.?|Table)\s*\d", text, re.I))
            )
            entry = _entry_from_block(
                block={**block, "index": block.get("index", index)},
                page_number=page_number,
                text=text,
                section_title=current_section_title,
                chunk_type="caption_candidate" if is_caption else "body_paragraph",
                source_heading_level=current_section_heading_level,
            )
            if entries and _should_merge_entries(entries[-1], entry):
                entries[-1] = _merge_entries(entries[-1], entry)
            else:
                entries.append(entry)
            continue

        if block_type != "list":
            continue

        list_items = block.get("list_items")
        if not isinstance(list_items, list):
            continue
        sub_type = str(block.get("sub_type", "")).strip().lower()
        if sub_type == "ref_text" or reference_section_active:
            for item_index, item in enumerate(list_items, start=1):
                if not isinstance(item, str):
                    continue
                text = _normalize_mineru_text(item)
                if not text:
                    continue
                entry = _entry_from_block(
                    block={
                        "type": "list",
                        "index": f"{index}:i{item_index}",
                        "bbox": block.get("bbox"),
                    },
                    page_number=page_number,
                    text=text,
                    section_title="References",
                    chunk_type="reference_entry",
                    source_heading_level=current_section_heading_level,
                )
                entries.extend(_split_reference_entry(entry))
            continue

        for item_index, item in enumerate(list_items, start=1):
            if not isinstance(item, str):
                continue
            text = _normalize_mineru_text(item)
            if not text:
                continue
            entry = _entry_from_block(
                block={
                    "type": "list",
                    "index": f"{index}:i{item_index}",
                    "bbox": block.get("bbox"),
                },
                page_number=page_number,
                text=text,
                section_title=current_section_title,
                chunk_type="body_paragraph",
                source_heading_level=current_section_heading_level,
            )
            if entries and _should_merge_entries(entries[-1], entry):
                entries[-1] = _merge_entries(entries[-1], entry)
            else:
                entries.append(entry)

    return entries


def _split_reference_entry(entry: MineruJsonEntry) -> list[MineruJsonEntry]:
    parts = [
        part.strip() for part in _REFERENCE_SPLIT_RE.split(entry.text) if part.strip()
    ]
    cleaned_parts: list[str] = []
    for part in parts:
        normalized = re.sub(r"\s+", " ", part).strip()
        if _looks_like_numbered_reference_start(normalized):
            cleaned_parts.append(normalized)
            continue
        if cleaned_parts:
            cleaned_parts[-1] = f"{cleaned_parts[-1]} {normalized}".strip()
            continue
        cleaned_parts.append(normalized)
    if len(cleaned_parts) <= 1:
        return [entry]
    return [
        MineruJsonEntry(
            text=text,
            page_number=entry.page_number,
            section_title=entry.section_title,
            chunk_type=entry.chunk_type,
            source_block_type=entry.source_block_type,
            source_block_id=f"{entry.source_block_id}:ref{index}",
            source_bboxes=entry.source_bboxes,
            source_heading_level=entry.source_heading_level,
            source_identity_json=entry.source_identity_json,
        )
        for index, text in enumerate(cleaned_parts, start=1)
    ]


def _looks_like_numbered_reference_start(text: str) -> bool:
    return bool(
        re.match(
            r"^(?:\d{1,3}[.)]|\[\d{1,3}\])\s+[A-Z][A-Za-z'鈥?-]{1,40}(?:,|\s+et\s+al\.)",
            text,
        )
    )


def _looks_like_precise_heading(text: str, text_level: object) -> bool:
    if _looks_like_section_heading(text):
        return True
    return text_level in {1, "1"}


def _block_sort_key(block: dict[str, Any]) -> tuple[float, float]:
    index = block.get("index", 0)
    try:
        index_value = float(index)
    except (TypeError, ValueError):
        index_value = 0.0
    bbox = _coerce_bbox(block.get("bbox"))
    return (index_value, bbox[1] if bbox else 0.0)


def _extract_block_text(block: dict[str, Any]) -> str:
    parts: list[str] = []
    lines = block.get("lines", [])
    if not isinstance(lines, list):
        return ""
    for line in sorted(
        (item for item in lines if isinstance(item, dict)), key=_line_sort_key
    ):
        spans = line.get("spans", [])
        line_parts: list[str] = []
        if not isinstance(spans, list):
            continue
        for span in spans:
            if not isinstance(span, dict):
                continue
            content = str(span.get("content", "")).strip()
            if content:
                line_parts.append(content)
        if line_parts:
            parts.append(" ".join(line_parts))
    return _normalize_mineru_text(" ".join(parts))


def _normalize_mineru_text(text: str) -> str:
    normalized = text
    normalized = re.sub(
        r"\\(?:mathbf|mathsf|mathrm|mathtt|boldsymbol|textsf|bf|sf)\s*\{\s*([^{}]*)\s*\}",
        lambda match: _collapse_spaced_formula_token(match.group(1)),
        normalized,
    )
    normalized = re.sub(
        r"_\s*\{\s*([^{}]+)\s*\}",
        lambda match: _collapse_spaced_formula_token(match.group(1)),
        normalized,
    )
    normalized = re.sub(
        r"\^\s*\{\s*([^{}]+)\s*\}",
        lambda match: _collapse_spaced_formula_token(match.group(1)),
        normalized,
    )
    normalized = normalized.replace("\\cdot", ".")
    normalized = normalized.replace("\\thinspace", " ")
    normalized = normalized.replace("\\left", " ").replace("\\right", " ")
    normalized = re.sub(r"\\[A-Za-z]+", " ", normalized)
    normalized = re.sub(r"\{\s*-\s*\}", "-", normalized)
    normalized = normalized.replace("{", " ").replace("}", " ")
    normalized = re.sub(
        r"\b(?:[A-Za-z0-9]\s+){1,}[A-Za-z0-9]\b",
        lambda match: _collapse_spaced_formula_token(match.group(0)),
        normalized,
    )
    normalized = re.sub(r"\b([A-Z]{1,6})\s+(\d)\b", r"\1\2", normalized)
    normalized = re.sub(r"(?<=[A-Za-z])\s*-\s*(?=\d)", "-", normalized)
    normalized = re.sub(r"\s+([,.;:!?)])", r"\1", normalized)
    normalized = re.sub(r"([(])\s+", r"\1", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _collapse_spaced_formula_token(text: str) -> str:
    pieces = [piece for piece in re.split(r"\s+", text.strip()) if piece]
    if not pieces:
        return ""
    if all(len(piece) == 1 and re.match(r"[A-Za-z0-9+\-]", piece) for piece in pieces):
        return "".join(pieces)
    return " ".join(pieces)


def _line_sort_key(line: dict[str, Any]) -> tuple[float, float]:
    index = line.get("index", 0)
    try:
        index_value = float(index)
    except (TypeError, ValueError):
        index_value = 0.0
    bbox = _coerce_bbox(line.get("bbox"))
    return (index_value, bbox[1] if bbox else 0.0)


def _entry_from_block(
    *,
    block: dict[str, Any],
    page_number: int,
    text: str,
    section_title: str,
    chunk_type: str,
    source_heading_level: str,
) -> MineruJsonEntry:
    block_index = str(block.get("index", "")).strip()
    original_id = str(block.get("source_block_id", "")).strip()
    block_type = (
        "local_pdf_text"
        if block.get("parser") == "pymupdf"
        else (str(block.get("type", "")).strip() or "text")
    )
    return MineruJsonEntry(
        text=text,
        page_number=page_number,
        section_title=section_title,
        chunk_type=chunk_type,
        source_block_type=block_type,
        source_block_id=original_id
        or (f"p{page_number}:b{block_index}" if block_index else f"p{page_number}"),
        source_bboxes=[bbox for bbox in [_coerce_bbox(block.get("bbox"))] if bbox],
        source_heading_level=source_heading_level,
        source_identity_json=json.dumps(
            [
                {
                    "kind": "declared_persistent"
                    if original_id and block.get("source_block_id_kind") == "persistent"
                    else "geometric"
                    if _coerce_bbox(block.get("bbox"))
                    else "positional",
                    "block_id": original_id
                    if block.get("source_block_id_kind") == "persistent"
                    else "",
                    "page_index": block.get("page_idx", page_number - 1),
                    "bbox": _coerce_bbox(block.get("bbox")),
                }
            ],
            sort_keys=True,
        ),
    )


def _should_merge_entries(previous: MineruJsonEntry, current: MineruJsonEntry) -> bool:
    if (
        previous.chunk_type != "body_paragraph"
        or current.chunk_type != "body_paragraph"
    ):
        return False
    if (
        previous.source_block_type == "local_pdf_text"
        or current.source_block_type == "local_pdf_text"
    ):
        return False
    if previous.page_number != current.page_number:
        return False
    if previous.section_title != current.section_title:
        return False
    if not _looks_like_incomplete_paragraph(previous.text):
        return False
    return _looks_like_continuation(current.text)


def _merge_entries(
    previous: MineruJsonEntry, current: MineruJsonEntry
) -> MineruJsonEntry:
    return MineruJsonEntry(
        text=f"{previous.text.rstrip()} {current.text.lstrip()}",
        page_number=previous.page_number,
        section_title=previous.section_title,
        chunk_type=previous.chunk_type,
        source_block_type=_join_unique_csv(
            [previous.source_block_type, current.source_block_type]
        ),
        source_block_id=",".join(
            filter(None, [previous.source_block_id, current.source_block_id])
        ),
        source_bboxes=[*previous.source_bboxes, *current.source_bboxes],
        source_heading_level=previous.source_heading_level,
        source_identity_json=json.dumps(
            json.loads(previous.source_identity_json or "[]")
            + json.loads(current.source_identity_json or "[]"),
            sort_keys=True,
        ),
    )


def _looks_like_incomplete_paragraph(text: str) -> bool:
    stripped = text.rstrip()
    if not stripped:
        return False
    if _TRANSITION_TAIL_RE.search(stripped):
        return True
    return stripped[-1] not in ".!?:"


def _looks_like_continuation(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    first_token = stripped.split()[0].lstrip("-").rstrip(",;:.)]")
    if not first_token:
        return False
    first_char = first_token[0]
    if first_char.islower() or first_char.isdigit() or first_char in "[({$":
        return True
    if _CHEMICAL_TOKEN_RE.match(first_token):
        return True
    return False


def _looks_like_section_heading(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > 180:
        return False
    normalized = _normalize_heading_text(stripped)
    return bool(
        _SECTION_PREFIX_RE.match(stripped) or _COMMON_SECTION_RE.match(normalized)
    )


def _normalize_heading_text(heading_text: str) -> str:
    return re.sub(r"\s+", " ", heading_text.casefold()).strip()


def _is_reference_heading(normalized_heading: str) -> bool:
    return normalized_heading == "references" or normalized_heading.endswith(
        " references"
    )


def _is_post_reference_heading(normalized_heading: str) -> bool:
    return bool(_POST_REFERENCE_HEADING_RE.match(normalized_heading))


def _coerce_bbox(value: object) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        return []
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError):
        return []


def _join_unique_csv(values: list[str]) -> str:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part and part not in seen:
                seen.add(part)
                unique.append(part)
    return ",".join(unique)
