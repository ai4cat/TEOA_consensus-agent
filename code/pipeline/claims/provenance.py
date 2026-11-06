from __future__ import annotations

import json
import re

from shared.text_cleaning import clean_text_for_model
from scoring.scoring.evidence_text import (
    focused_evidence_text,
    paragraph_evidence_text,
)


def _parse_origin_evidence_items(value: object) -> list[dict[str, object]]:
    text = str(value or "").strip()
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


def _attach_source_index_chunks(
    normalized_claims: list[dict[str, object]],
    support_rows: list[dict[str, object]],
    chunk_rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Attach the one source chunk used for claim-level evidence scoring.

    Step 3 also writes many retrieval/support rows for frequency counting. Those
    rows answer "how often and where does this claim appear?"  They must not be
    treated as the evidence-strength input.  Evidence strength is scored from
    exactly one source_index_chunk per child claim, selected here.
    """

    del support_rows
    chunk_by_id = {
        str(row.get("chunk_id", "")).strip(): row
        for row in chunk_rows
        if str(row.get("chunk_id", "")).strip()
    }

    enriched_rows: list[dict[str, object]] = []
    for claim in normalized_claims:
        updated = dict(claim)
        source = _choose_origin_source_chunk(updated, chunk_by_id)
        if not source:
            source = _missing_source_chunk()

        updated.update(source)
        enriched_rows.append(updated)
    return enriched_rows


def _choose_origin_source_chunk(
    claim: dict[str, object],
    chunk_by_id: dict[str, dict[str, object]],
) -> dict[str, object]:
    for origin in _parse_origin_evidence_items(claim.get("origin_evidence_json", "")):
        chunk_id = str(origin.get("chunk_id", "")).strip()
        if not chunk_id or chunk_id not in chunk_by_id:
            continue
        chunk = chunk_by_id[chunk_id]
        return _source_chunk_fields(
            chunk_id=chunk_id,
            chunk=chunk,
            quote=str(origin.get("raw_claim_text", "")).strip(),
            status="extracted",
            reason="origin_evidence_json first valid chunk",
            fallback_row=origin,
        )
    for chunk_id in _origin_chunk_ids(claim):
        if chunk_id not in chunk_by_id:
            continue
        chunk = chunk_by_id[chunk_id]
        return _source_chunk_fields(
            chunk_id=chunk_id,
            chunk=chunk,
            quote="",
            status="extracted",
            reason="origin_chunk_ids first valid chunk",
            fallback_row={
                "doc_id": claim.get("origin_doc_ids", ""),
            },
        )
    return {}


def _origin_chunk_ids(claim: dict[str, object]) -> list[str]:
    raw_value = str(claim.get("origin_chunk_ids", "")).strip()
    if not raw_value:
        return []
    seen: set[str] = set()
    chunk_ids: list[str] = []
    for chunk_id in re.split(r"[;,]\s*", raw_value):
        normalized = chunk_id.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        chunk_ids.append(normalized)
    return chunk_ids


def _source_chunk_fields(
    *,
    chunk_id: str,
    chunk: dict[str, object],
    quote: str,
    status: str,
    reason: str,
    fallback_row: dict[str, object],
) -> dict[str, object]:
    context = str(
        paragraph_evidence_text(fallback_row)
        or fallback_row.get("expanded_context", "")
        or chunk.get("text", "")
        or chunk.get("clean_text", "")
        or quote
    ).strip()
    return {
        "source_chunk_id": chunk_id,
        "source_doc_id": str(fallback_row.get("doc_id", "") or chunk.get("doc_id", "")),
        "source_page_number": str(
            fallback_row.get("page_number", "") or chunk.get("page_number", "")
        ),
        "source_paragraph_id": str(
            fallback_row.get("paragraph_id", "") or chunk.get("paragraph_id", "")
        ),
        "source_context": context,
        "source_chunk_status": status,
        "source_chunk_selection_reason": reason,
    }


def _missing_source_chunk() -> dict[str, object]:
    return {
        "source_chunk_id": "",
        "source_doc_id": "",
        "source_page_number": "",
        "source_paragraph_id": "",
        "source_context": "",
        "source_chunk_status": "missing_primary_source",
        "source_chunk_selection_reason": "no valid origin_evidence_json chunk found in chunk index",
    }


def _build_origin_occurrence_rows(
    normalized_claims: list[dict[str, object]],
    chunk_rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    chunk_by_id = {
        str(row.get("chunk_id", "")).strip(): row
        for row in chunk_rows
        if str(row.get("chunk_id", "")).strip()
    }
    occurrence_rows: list[dict[str, object]] = []
    for claim in normalized_claims:
        normalized_claim_id = str(claim.get("normalized_claim_id", "")).strip()
        normalized_claim_text = str(claim.get("normalized_claim_text", "")).strip()
        if not normalized_claim_id:
            continue

        seen_keys: set[tuple[str, str, str, str]] = set()
        claim_rows: list[dict[str, object]] = []
        for origin_rank, origin in enumerate(
            _parse_origin_evidence_items(claim.get("origin_evidence_json", "")), start=1
        ):
            chunk_id = str(origin.get("chunk_id", "")).strip()
            doc_id = str(origin.get("doc_id", "")).strip()
            raw_claim_id = str(origin.get("raw_claim_id", "")).strip()
            dedupe_key = (normalized_claim_id, raw_claim_id, chunk_id, doc_id)
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)

            chunk = chunk_by_id.get(chunk_id, {})
            raw_context = (
                paragraph_evidence_text(origin)
                or str(chunk.get("text", "") or chunk.get("clean_text", "")).strip()
            )
            evidence_text = focused_evidence_text(origin) or raw_context
            clean_context = (
                clean_text_for_model(raw_context or evidence_text)
                or raw_context
                or evidence_text
            )
            claim_rows.append(
                {
                    "normalized_claim_id": normalized_claim_id,
                    "normalized_claim_text": normalized_claim_text,
                    "chunk_id": chunk_id,
                    "doc_id": doc_id or str(chunk.get("doc_id", "")).strip(),
                    "page_number": str(
                        origin.get("page_number", "") or chunk.get("page_number", "")
                    ).strip(),
                    "paragraph_id": str(
                        origin.get("paragraph_id", "") or chunk.get("paragraph_id", "")
                    ).strip(),
                    "char_start": str(
                        origin.get("char_start", "") or chunk.get("char_start", "")
                    ).strip(),
                    "char_end": str(
                        origin.get("char_end", "") or chunk.get("char_end", "")
                    ).strip(),
                    "evidence_candidate_text": evidence_text,
                    "expanded_context": raw_context or evidence_text,
                    "clean_context": clean_context,
                    "retrieval_score": "",
                    "similarity_score": "",
                    "support_cue_score": "",
                    "support_score": "1.0",
                    "support_label": "support",
                    "selection_source": "origin_occurrence",
                    "origin_raw_claim_id": raw_claim_id,
                    "origin_order": origin_rank,
                }
            )

        for rank, row in enumerate(claim_rows, start=1):
            row["rank_in_claim"] = rank
            occurrence_rows.append(row)

    return occurrence_rows
