from __future__ import annotations

import json
import re


_REFERENCE_HEADER_RE = re.compile(r"(?im)^\s*(references|bibliography)\s*:?\s*$")
_REFERENCE_ITEM_RE = re.compile(r"^\s*(?:\[(\d+)\]|\((\d+)\)|(\d+)[\].)])\s*(.+?)\s*$")
_INLINE_REFERENCE_ITEM_BOUNDARY_RE = re.compile(
    r"(?<!^)(?<!\n)\s+(?=(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}[\].)])\s*[A-Z])"
)
_CITATION_NUMBER_RE = re.compile(
    r"\[\s*(\d+(?:\s*[-,;]\s*\d+)*)\s*\]|\(\s*(\d+(?:\s*[-,;]\s*\d+)*)\s*\)"
)
_TAIL_REFERENCE_CUE_RE = re.compile(
    r"(?i)\b(reference|references|citation|citations|bibliography|works cited)\b"
)
_NON_REFERENCE_SECTION_HEADING_RE = re.compile(
    r"(?i)^(appendix|appendices|notes|footnotes|supplementary|supplementary materials?)$"
)


def parse_reference_entries(fulltext: str) -> tuple[list[dict[str, object]], str]:
    text = str(fulltext or "")
    header_match = _REFERENCE_HEADER_RE.search(text)
    if header_match:
        entries = _parse_reference_block(text[header_match.end() :])
        if entries:
            return entries, "references_header"

    tail_entries = _parse_tail_run(text)
    if tail_entries:
        return tail_entries, "tail_run"
    return [], ""


def build_raw_reference_scheme(
    *,
    chunk_row: dict[str, object],
    fulltext: str = "",
    raw_claim_text: str = "",
    citation_numbers: object = "",
    citation_evidence_span: str = "",
) -> dict[str, object]:
    chunk_text = _chunk_text(chunk_row)
    reference_entries, _parse_mode = parse_reference_entries(fulltext)
    reference_map = {int(entry["ref_num"]): entry for entry in reference_entries}
    source_quote_exact = str(chunk_row.get("source_quote_exact", "")).strip()
    citation_span = (
        str(citation_evidence_span or "").strip()
        or source_quote_exact
        or raw_claim_text
    )
    requested_numbers = _normalize_citation_numbers(citation_numbers)
    citation_numbers = _validated_claim_citation_numbers(
        citation_text=citation_span,
        raw_claim_text=raw_claim_text,
        llm_numbers=requested_numbers,
        reference_map=reference_map,
    )
    citation_source = "citation_numbers_validated" if citation_numbers else ""
    if not citation_numbers:
        local_numbers = _parse_citation_numbers(citation_span)
        if not local_numbers:
            chunk_local_span = _claim_local_citation_text(chunk_text, raw_claim_text)
            if chunk_local_span:
                citation_span = chunk_local_span
            local_numbers = _parse_citation_numbers(citation_span)
        if (
            reference_map
            and local_numbers
            and not any(number in reference_map for number in local_numbers)
        ):
            citation_numbers = []
            citation_source = "unmapped_claim_local_citation"
        else:
            citation_numbers, citation_source, citation_span = (
                _backfill_claim_citation_numbers(
                    chunk_text=chunk_text,
                    raw_claim_text=raw_claim_text,
                    citation_evidence_span=citation_span,
                    reference_map=reference_map,
                )
            )
    mapped_entries = [
        reference_map[number] for number in citation_numbers if number in reference_map
    ]
    references_used = [
        {
            "reference_number": str(entry["ref_num"]),
            "reference_text": str(entry["entry_text"]),
        }
        for entry in mapped_entries
    ]
    citation_number_texts = [str(number) for number in citation_numbers]
    evidence_scheme = {
        "page_number": str(chunk_row.get("page_number", "")).strip(),
        "paragraph_id": str(chunk_row.get("paragraph_id", "")).strip(),
        "evidence_unit_type": "paragraph_chunk",
        "evidence_chunk_text": chunk_text,
        "source_quote_exact": source_quote_exact or citation_span,
        "citation_evidence_span": citation_span,
        "citation_numbers": citation_number_texts,
        "references_used": references_used,
        "citation_source": citation_source,
    }
    return {
        "citation_numbers": ";".join(citation_number_texts),
        "reference_used": json.dumps(references_used, ensure_ascii=False),
        "evidence_scheme_json": json.dumps(evidence_scheme, ensure_ascii=False),
    }


def _backfill_claim_citation_numbers(
    *,
    chunk_text: str,
    raw_claim_text: str,
    citation_evidence_span: str,
    reference_map: dict[int, dict[str, object]],
) -> tuple[list[int], str, str]:
    available_reference_numbers = set(reference_map)
    candidates = _claim_citation_candidate_spans(
        chunk_text=chunk_text,
        raw_claim_text=raw_claim_text,
        citation_evidence_span=citation_evidence_span,
    )
    for source, span in candidates:
        numbers = _parse_citation_numbers(span)
        if available_reference_numbers:
            numbers = [
                number for number in numbers if number in available_reference_numbers
            ]
        if numbers:
            return numbers, source, span
    return [], "", citation_evidence_span


def _claim_citation_candidate_spans(
    *, chunk_text: str, raw_claim_text: str, citation_evidence_span: str
) -> list[tuple[str, str]]:
    """Use only source-local literal spans; do not infer citations from chemistry words."""
    source = " ".join(str(chunk_text or "").split())
    spans = []
    for label, value in (
        ("citation_evidence_span_backfill", citation_evidence_span),
        ("literal_claim_span_backfill", raw_claim_text),
    ):
        span = " ".join(str(value or "").split())
        if span and span in source:
            spans.append((label, span))
    return _dedupe_candidate_spans(spans)


def _dedupe_candidate_spans(spans: list[tuple[str, str]]) -> list[tuple[str, str]]:
    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source, span in spans:
        normalized = " ".join(str(span or "").split())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append((source, normalized))
    return deduped


def _normalize_citation_numbers(raw_value: object) -> list[int]:
    if isinstance(raw_value, list):
        values = raw_value
    elif isinstance(raw_value, tuple):
        values = list(raw_value)
    else:
        text = str(raw_value or "").strip()
        if not text:
            return []
        values = re.split(r"[;,]\s*", text)
    numbers: list[int] = []
    seen: set[int] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        for parsed in _parse_citation_numbers(f"[{text}]"):
            if parsed not in seen:
                seen.add(parsed)
                numbers.append(parsed)
    return numbers


def _validated_claim_citation_numbers(
    *,
    citation_text: str,
    raw_claim_text: str = "",
    llm_numbers: list[int],
    reference_map: dict[int, dict[str, object]],
) -> list[int]:
    if not llm_numbers:
        return []
    local_numbers = set(_parse_citation_numbers(citation_text))
    local_numbers.update(_parse_citation_numbers(raw_claim_text))
    validated: list[int] = []
    for number in llm_numbers:
        if number not in local_numbers:
            continue
        if reference_map and number not in reference_map:
            continue
        validated.append(number)
    return validated


def _claim_local_citation_text(chunk_text: str, raw_claim_text: str = "") -> str:
    chunk = str(chunk_text or "")
    claim = str(raw_claim_text or "").strip()
    if not chunk:
        return ""
    if not claim:
        return chunk
    sentences = _split_sentences_with_local_citations(chunk)
    best_sentence = _best_matching_sentence(sentences, claim)
    if best_sentence:
        return best_sentence
    return claim


def _split_sentences_with_local_citations(text: str) -> list[str]:
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return []
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9$\\])", normalized)
    return [part.strip() for part in parts if part.strip()]


def _best_matching_sentence(sentences: list[str], claim: str) -> str:
    best_index = _best_matching_sentence_index(sentences, claim)
    return sentences[best_index] if best_index is not None else ""


def _best_matching_sentence_index(sentences: list[str], claim: str) -> int | None:
    needle = " ".join(str(claim or "").split())
    if not needle:
        return None
    for index, sentence in enumerate(sentences):
        if needle in " ".join(sentence.split()):
            return index
    return None


def _parse_reference_block(block_text: str) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    current_num: int | None = None
    current_lines: list[str] = []
    saw_blank_after_entry = False
    normalized_block_text = _split_inline_reference_items(block_text)
    for raw_line in normalized_block_text.splitlines():
        line = raw_line.strip()
        if not line:
            if current_num is not None and current_lines:
                saw_blank_after_entry = True
            continue
        item_match = _REFERENCE_ITEM_RE.match(line)
        if item_match:
            if current_num is not None and current_lines:
                entries.append(
                    {
                        "ref_num": current_num,
                        "entry_text": " ".join(current_lines).strip(),
                    }
                )
            current_num = int(
                item_match.group(1) or item_match.group(2) or item_match.group(3)
            )
            current_lines = [item_match.group(4).strip()]
            saw_blank_after_entry = False
            continue
        if saw_blank_after_entry and current_num is not None and current_lines:
            break
        if (
            current_num is not None
            and current_lines
            and _NON_REFERENCE_SECTION_HEADING_RE.match(line)
        ):
            break
        if current_num is None:
            continue
        if not _line_looks_like_reference_continuation(line):
            break
        current_lines.append(line)
    if current_num is not None and current_lines:
        entries.append(
            {"ref_num": current_num, "entry_text": " ".join(current_lines).strip()}
        )
    return entries


def _split_inline_reference_items(block_text: str) -> str:
    return _INLINE_REFERENCE_ITEM_BOUNDARY_RE.sub("\n", block_text)


def _parse_tail_run(fulltext: str) -> list[dict[str, object]]:
    lines = _split_inline_reference_items(fulltext).splitlines()
    if not lines:
        return []
    start_index = _reference_run_search_start(lines)
    tail_lines = lines[start_index:]
    candidate_start_indexes = [
        index
        for index, line in enumerate(tail_lines)
        if _REFERENCE_ITEM_RE.match(line.strip())
        and not _previous_nonblank_line_is_reference_item(tail_lines, index)
    ]
    for index in reversed(candidate_start_indexes):
        candidate_lines = tail_lines[index:]
        entries = _parse_reference_block("\n".join(candidate_lines))
        if entries and _tail_run_looks_like_references(candidate_lines, entries):
            return entries
    return []


def _reference_run_search_start(lines: list[str]) -> int:
    """Search broadly because MinerU sometimes drops the bibliography heading."""
    if len(lines) < 80:
        return 0
    return max(0, len(lines) // 4)


def _previous_nonblank_line_is_reference_item(lines: list[str], index: int) -> bool:
    for previous_index in range(index - 1, -1, -1):
        previous_line = lines[previous_index].strip()
        if not previous_line:
            continue
        return bool(_REFERENCE_ITEM_RE.match(previous_line))
    return False


def _parse_origin_evidence_items(raw_value: object) -> list[dict[str, object]]:
    text = str(raw_value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _chunk_text(chunk_row: dict[str, object]) -> str:
    return str(
        chunk_row.get("text", "")
        or chunk_row.get("clean_text", "")
        or chunk_row.get("source_context", "")
    ).strip()


def _parse_citation_numbers(text: str) -> list[int]:
    seen: set[int] = set()
    numbers: list[int] = []
    normalized_text = _normalize_citation_text(text)
    for match in _CITATION_NUMBER_RE.finditer(normalized_text):
        if _looks_like_numbered_list_marker(normalized_text, match):
            continue
        raw_group = match.group(1) or match.group(2) or ""
        for part in re.split(r"[,;]\s*", raw_group):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                bounds = [value.strip() for value in part.split("-", maxsplit=1)]
                if len(bounds) == 2 and bounds[0].isdigit() and bounds[1].isdigit():
                    start, end = int(bounds[0]), int(bounds[1])
                    for number in range(min(start, end), max(start, end) + 1):
                        if number not in seen:
                            seen.add(number)
                            numbers.append(number)
                continue
            if part.isdigit():
                number = int(part)
                if _looks_like_year_token(number):
                    continue
                if number not in seen:
                    seen.add(number)
                    numbers.append(number)
    return numbers


def _looks_like_numbered_list_marker(text: str, match: re.Match[str]) -> bool:
    if match.group(1):
        return False
    after = text[match.end() : match.end() + 24]
    before = text[max(0, match.start() - 3) : match.start()]
    if re.match(r"\s+[A-Za-z]", after):
        return True
    if before.endswith("; ") and re.match(r"\s*[A-Za-z]", after):
        return True
    return False


def _looks_like_year_token(number: int) -> bool:
    return 1800 <= number <= 2099


def _normalize_citation_text(text: str) -> str:
    normalized = str(text or "")
    for dash_char in (
        "\u2010",
        "\u2011",
        "\u2012",
        "\u2013",
        "\u2014",
        "\u2212",
        "\ufe58",
        "\ufe63",
        "\uff0d",
    ):
        normalized = normalized.replace(dash_char, "-")
    return normalized


def _tail_run_looks_like_references(
    tail_lines: list[str],
    entries: list[dict[str, object]],
) -> bool:
    if any(
        _entry_looks_like_reference(str(entry.get("entry_text", "")))
        for entry in entries
    ):
        return True
    prose_before_entries: list[str] = []
    for raw_line in tail_lines:
        line = raw_line.strip()
        if not line:
            continue
        if _REFERENCE_ITEM_RE.match(line):
            break
        prose_before_entries.append(line)
    return any(_TAIL_REFERENCE_CUE_RE.search(line) for line in prose_before_entries)


def _line_looks_like_reference_continuation(line: str) -> bool:
    text = str(line or "").strip()
    if not text:
        return False
    lowered = text.lower()
    return (
        bool(re.search(r"\b(19|20)\d{2}\b", text))
        or "doi" in lowered
        or "http://" in lowered
        or "https://" in lowered
        or "et al" in lowered
        or bool(
            re.search(
                r"\b(?:chem|catal|mater|phys|energy|environ|j\.|acs|angew|adv|appl|nat|sci)\b",
                lowered,
            )
        )
        or bool(re.match(r"^[a-z,;)]", text))
    )


def _entry_looks_like_reference(entry_text: str) -> bool:
    text = str(entry_text or "").strip()
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "reference",
            "doi",
            "http://",
            "https://",
            "et al.",
            "vol.",
            "journal",
        )
    ) or bool(re.search(r"\b(19|20)\d{2}\b", text))
