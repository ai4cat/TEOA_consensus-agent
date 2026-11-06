from __future__ import annotations

import json


def _first_nonempty_text(row: dict[str, object], *fieldnames: str) -> str:
    for fieldname in fieldnames:
        value = str(row.get(fieldname, "")).strip()
        if value:
            return value
    return ""


def focused_evidence_text(row: dict[str, object]) -> str:
    scheme = _evidence_scheme(row)
    raw_claim_text = str(row.get("raw_claim_text", "")).strip()
    if raw_claim_text:
        return raw_claim_text
    if scheme:
        return str(scheme.get("raw_claim_text", "")).strip()
    return _first_nonempty_text(
        row,
        "source_quote",
        "evidence_candidate_text",
        "raw_claim_quote",
        "claim_text",
        "clean_text",
        "text",
    )


def paragraph_evidence_text(row: dict[str, object]) -> str:
    scheme = _evidence_scheme(row)
    if scheme:
        text = str(scheme.get("evidence_chunk_text", "")).strip()
        if text:
            return text
    return _first_nonempty_text(
        row,
        "source_context",
        "expanded_context",
        "raw_claim_context",
        "clean_context",
        "clean_text",
        "text",
        "evidence_candidate_text",
        "source_quote",
        "raw_claim_quote",
        "claim_text",
    )


def _evidence_scheme(row: dict[str, object]) -> dict[str, object]:
    raw_value = row.get("evidence_scheme_json", "")
    if isinstance(raw_value, dict):
        return raw_value
    text = str(raw_value or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
