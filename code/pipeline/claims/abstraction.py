from __future__ import annotations

import json
import os
import re
from pathlib import Path

from dotenv import dotenv_values

from shared.llm.transport import _request_responses_api
from shared.selected_entity import entity_instruction
from scoring.scoring.audit import load_llm_env_if_available

DOCUMENT_ABSTRACTION_FIELDS = [
    "doc_id",
    "mechanism_summary",
    "selectivity_summary",
    "target_role_summary",
    "controls_summary",
    "contradictions_summary",
    "design_rule_summary",
    "limitations_summary",
    "supporting_chunk_ids_json",
]
_DOCUMENT_ABSTRACTION_LLM_ROLE = "STEP2B"
_DEFAULT_DOCUMENT_ABSTRACTION_MODEL = "gpt-5.4-mini"
_DEFAULT_DOCUMENT_ABSTRACTION_MAX_OUTPUT_TOKENS = 1400

_MAX_SOURCE_CHUNKS = 18
_MAX_SOURCE_CHARS = 16000


def build_document_abstraction_row(
    doc_id: str, chunk_rows: list[dict[str, str]]
) -> dict[str, str]:
    selected_rows = _select_document_chunks(chunk_rows)
    if not selected_rows:
        return _empty_document_abstraction_row(doc_id)

    llm_env = _load_document_abstraction_llm_env()
    prompt = _build_document_abstraction_prompt(doc_id, selected_rows)
    try:
        content = _request_responses_api(
            api_key=str(llm_env["LLM_API_KEY"]),
            base_url=str(llm_env["LLM_API_BASE_URL"]),
            model_name=str(llm_env["LLM_MODEL_NAME"]),
            prompt=prompt,
            max_output_tokens=_DEFAULT_DOCUMENT_ABSTRACTION_MAX_OUTPUT_TOKENS,
        )
    except RuntimeError as exc:
        raise RuntimeError(
            f"document abstraction remote request failed for {doc_id}: {exc}"
        ) from exc
    payload = _parse_document_abstraction_payload(content)
    if not payload:
        raise RuntimeError(
            f"document abstraction remote response could not be parsed for {doc_id}: raw={content[:300]}"
        )

    allowed_chunk_ids = {
        str(row.get("chunk_id", "")).strip()
        for row in selected_rows
        if str(row.get("chunk_id", "")).strip()
    }
    supporting_chunk_ids = [
        chunk_id
        for chunk_id in _coerce_string_list(payload.get("supporting_chunk_ids"))
        if chunk_id in allowed_chunk_ids
    ]

    return {
        "doc_id": doc_id,
        "mechanism_summary": _coerce_summary_text(payload.get("mechanism_summary")),
        "selectivity_summary": _coerce_summary_text(payload.get("selectivity_summary")),
        "target_role_summary": _coerce_summary_text(payload.get("target_role_summary")),
        "controls_summary": _coerce_summary_text(payload.get("controls_summary")),
        "contradictions_summary": _coerce_summary_text(
            payload.get("contradictions_summary")
        ),
        "design_rule_summary": _coerce_summary_text(payload.get("design_rule_summary")),
        "limitations_summary": _coerce_summary_text(payload.get("limitations_summary")),
        "supporting_chunk_ids_json": json.dumps(
            supporting_chunk_ids, ensure_ascii=False
        ),
    }


def _load_document_abstraction_llm_env() -> dict[str, str]:
    resolved = load_llm_env_if_available(llm_role=_DOCUMENT_ABSTRACTION_LLM_ROLE)
    resolved["LLM_MODEL_NAME"] = _resolve_document_abstraction_model_name()
    missing = [
        key
        for key in ("LLM_API_KEY", "LLM_API_BASE_URL")
        if not str(resolved.get(key, "")).strip()
    ]
    if missing:
        raise RuntimeError(
            "Missing required document abstraction LLM configuration "
            f"for {_DOCUMENT_ABSTRACTION_LLM_ROLE}: {', '.join(missing)}"
        )
    return resolved


def _resolve_document_abstraction_model_name() -> str:
    role_key = f"{_DOCUMENT_ABSTRACTION_LLM_ROLE}_LLM_MODEL_NAME"
    role_env_value = os.getenv(role_key, "").strip()
    if role_env_value:
        return role_env_value
    for dotenv_path in _candidate_dotenv_paths():
        value = dotenv_values(dotenv_path).get(role_key)
        if value:
            return str(value).strip()
    return _DEFAULT_DOCUMENT_ABSTRACTION_MODEL


def _candidate_dotenv_paths() -> list[Path]:
    current = Path.cwd().resolve()
    candidates: list[Path] = []
    seen: set[Path] = set()
    for directory in (current, *current.parents):
        path = directory / ".env"
        if path in seen:
            continue
        seen.add(path)
        if path.exists():
            candidates.append(path)
    return candidates


def _empty_document_abstraction_row(doc_id: str) -> dict[str, str]:
    return {
        "doc_id": doc_id,
        "mechanism_summary": "",
        "selectivity_summary": "",
        "target_role_summary": "",
        "controls_summary": "",
        "contradictions_summary": "",
        "design_rule_summary": "",
        "limitations_summary": "",
        "supporting_chunk_ids_json": "[]",
    }


def _select_document_chunks(chunk_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Take a position-stratified document overview without scientific word scores."""
    rows = [
        dict(r)
        for r in chunk_rows
        if r.get("chunk_type") != "reference_entry"
        and str(r.get("text") or r.get("clean_text") or "").strip()
    ]
    if len(rows) > _MAX_SOURCE_CHUNKS:
        indices = [
            round(i * (len(rows) - 1) / (_MAX_SOURCE_CHUNKS - 1))
            for i in range(_MAX_SOURCE_CHUNKS)
        ]
        rows = [rows[i] for i in indices]
    for row in rows:
        row["clean_text"] = str(row.get("text") or row.get("clean_text") or "")[
            : max(1, _MAX_SOURCE_CHARS // max(1, len(rows)))
        ]
    return rows


def _build_document_abstraction_prompt(
    doc_id: str, chunk_rows: list[dict[str, str]]
) -> str:
    chunk_pack = "\n".join(
        f"- {row.get('chunk_id', '')}: [p{row.get('page_number', '')}] {str(row.get('clean_text') or row.get('text', '')).strip()}"
        for row in chunk_rows
    )
    return (
        entity_instruction()
        + "You are building a document-level evidence abstraction for a chemistry claim-audit pipeline.\n"
        "Return JSON only with keys: mechanism_summary, selectivity_summary, target_role_summary, controls_summary, "
        "contradictions_summary, design_rule_summary, limitations_summary, supporting_chunk_ids.\n"
        "These summaries are used downstream for impact and evidence scoring.\n"
        "Summarize only what is explicitly supported by the provided chunks.\n"
        "In target_role_summary, target means only the selected entity named above, including its supplied aliases. "
        "Describe that entity's source-supported role and identify it by name. "
        "Do not substitute the main catalyst, material, photosensitizer, or another component for the target. "
        "If these chunks do not support a role for the selected entity, return an empty target_role_summary. "
        "Other summary fields may describe the wider paper, while keeping each component's role distinct.\n"
        "Do not generalize a single-system observation into a field-wide rule.\n"
        "State whether reported roles and effects are directly tested, indirectly inferred, or only assumed.\n"
        "Summarize the supplied evidence without privileging a particular role or expecting a particular conclusion.\n"
        "Highlight direct evidence types when present, such as matched controls, substitutions, exclusion tests, isotope labels, operando measurements, or spectroscopy.\n"
        "Each summary may use one or two short sentences and should stay under 80 words.\n"
        "Use empty string when the paper does not support that category.\n"
        "supporting_chunk_ids must be a JSON array of 1-8 exact chunk ids copied from the chunk pack.\n"
        "Do not cite chunk ids inside the summaries themselves.\n\n"
        f"Document ID: {doc_id}\n"
        f"Chunk Pack:\n{chunk_pack}\n"
    )


def _parse_document_abstraction_payload(content: str) -> dict[str, object]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _coerce_summary_text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def _coerce_string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [
            " ".join(str(item).split()).strip() for item in value if str(item).strip()
        ]
    return []
