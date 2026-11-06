"""Validate and normalize structured claim-model output."""

from __future__ import annotations

import json
import re

from shared.llm.transport import _extract_response_text




def _parse_generic_json_object(content: str) -> dict[str, object]:
    stripped = content.strip()
    if not stripped:
        return {}
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        extracted_text = _extract_response_text(payload)
        if extracted_text:
            return _parse_generic_json_object(extracted_text)
        return payload

    match = re.search(r"\{.*\}", stripped, re.S)
    if not match:
        return {}
    try:
        nested_payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    if isinstance(nested_payload, dict):
        return nested_payload
    return {}


def _parse_json_payload(content: str) -> dict[str, object]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        payload = None

    if isinstance(payload, dict):
        candidate = _coerce_structured_output(payload)
        if candidate:
            return candidate

    match = re.search(r"\{.*\}", content, re.S)
    if match:
        try:
            nested_payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            nested_payload = None
        if isinstance(nested_payload, dict):
            return _coerce_structured_output(
                nested_payload
            ) or _salvage_structured_output(content)
    return _salvage_structured_output(content)


def _coerce_structured_output(payload: dict[str, object]) -> dict[str, object]:
    expected_keys = {
        "evidence_strength_llm",
        "impact_score_llm",
        "confidence",
        "key_passage",
        "reasoning",
        "gap",
    }
    primary_keys = {
        "evidence_strength_llm",
        "impact_score_llm",
        "confidence",
        "key_passage",
    }
    if primary_keys.issubset(payload.keys()):
        return payload
    extracted_text = _extract_response_text(payload)
    if extracted_text:
        return _parse_json_payload(extracted_text)
    nested = _find_expected_dict(payload, expected_keys)
    return nested or {}


def _find_expected_dict(value: object, expected_keys: set[str]) -> dict[str, object]:
    primary_keys = {
        "evidence_strength_llm",
        "impact_score_llm",
        "confidence",
        "key_passage",
    }
    if isinstance(value, dict):
        if primary_keys.issubset(value.keys()) or expected_keys.issubset(value.keys()):
            return value
        for nested_value in value.values():
            nested = _find_expected_dict(nested_value, expected_keys)
            if nested:
                return nested
    if isinstance(value, list):
        for item in value:
            nested = _find_expected_dict(item, expected_keys)
            if nested:
                return nested
    return {}


def _salvage_structured_output(content: str) -> dict[str, object]:
    primary_keys = {
        "evidence_strength_llm",
        "impact_score_llm",
        "confidence",
        "key_passage",
    }
    salvaged: dict[str, object] = {}
    for key in ("evidence_strength_llm", "impact_score_llm"):
        match = re.search(rf'"{key}"\s*:\s*(\d+)', content)
        if match:
            salvaged[key] = int(match.group(1))
    confidence_match = re.search(
        r'"confidence"\s*:\s*("(?P<quoted>(?:[^"\\]|\\.)*)"|(?P<numeric>[-+]?\d*\.?\d+))',
        content,
    )
    if confidence_match:
        quoted = confidence_match.group("quoted")
        salvaged["confidence"] = (
            _decode_json_string(quoted)
            if quoted is not None
            else confidence_match.group("numeric")
        )
    for key in ("key_passage", "reasoning", "gap"):
        match = re.search(rf'"{key}"\s*:\s*"(?P<value>(?:[^"\\]|\\.)*)"', content)
        if match:
            salvaged[key] = _decode_json_string(match.group("value"))
    for key in (
        "evidence_type",
        "evidence_chain_summary",
        "untested_assumptions",
        "falsification_route",
    ):
        match = re.search(rf'"{key}"\s*:\s*"(?P<value>(?:[^"\\]|\\.)*)"', content)
        if match:
            salvaged[key] = _decode_json_string(match.group("value"))
    for key in ("why_not_lower", "why_not_higher", "decision_relevance"):
        match = re.search(rf'"{key}"\s*:\s*"(?P<value>(?:[^"\\]|\\.)*)"', content)
        if match:
            salvaged[key] = _decode_json_string(match.group("value"))
    if primary_keys.issubset(salvaged.keys()):
        salvaged.setdefault("reasoning", "")
        salvaged.setdefault("gap", "")
        salvaged.setdefault("evidence_type", "")
        salvaged.setdefault("evidence_chain_summary", "")
        salvaged.setdefault("untested_assumptions", "")
        salvaged.setdefault("falsification_route", "")
        salvaged.setdefault("why_not_lower", "")
        salvaged.setdefault("why_not_higher", "")
        salvaged.setdefault("decision_relevance", "")
        salvaged.setdefault("review_required", True)
        salvaged.setdefault("cited_chunk_ids", [])
        salvaged.setdefault("impact_checks", _default_impact_checks())
        salvaged.setdefault("evidence_checks", _default_evidence_checks())
        return salvaged
    return {}


def _coerce_citation_numbers(raw_value: object) -> str:
    if isinstance(raw_value, list):
        raw_parts = [str(value).strip() for value in raw_value if str(value).strip()]
    else:
        text = str(raw_value or "").strip()
        if not text:
            return ""
        raw_parts = [
            part.strip() for part in re.split(r"[;,]\s*", text) if part.strip()
        ]

    cleaned_parts: list[str] = []
    seen: set[object] = set()
    for raw_part in raw_parts:
        normalized = raw_part.replace("–", "-").replace("—", "-").strip()
        if not _looks_like_bibliography_citation_token(normalized):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        cleaned_parts.append(normalized)
    return ";".join(cleaned_parts)


def _looks_like_bibliography_citation_token(token: str) -> bool:
    lowered = token.lower().strip()
    if not lowered:
        return False
    if any(
        marker in lowered
        for marker in (
            "fig",
            "figure",
            "table",
            "scheme",
            "eq",
            "equation",
            "supp",
            "appendix",
        )
    ):
        return False
    compact = "".join(ch for ch in lowered if not ch.isspace())
    if not compact:
        return False
    if compact.count("-") > 1:
        return False
    if "-" in compact:
        left, right = compact.split("-", maxsplit=1)
        return bool(left and right and left.isdigit() and right.isdigit())
    return compact.isdigit()


def _parse_chunk_claim_processor_payload(
    content: str, *, max_claims: int
) -> list[dict[str, str]]:
    payload = _parse_json_or_response_payload(content)
    claims = payload.get("claims")
    if not isinstance(claims, list):
        return []
    parsed: list[dict[str, str]] = []
    seen: set[object] = set()
    for item in claims:
        if not isinstance(item, dict):
            continue
        raw_claim_text = _normalize_claim_text(str(item.get("raw_claim_text", "")))
        if not raw_claim_text:
            continue
        source_quote_exact = (
            str(item.get("source_quote_exact", "")).strip()
            or str(item.get("citation_evidence_span", "")).strip()
        )
        dedupe_key = (raw_claim_text, source_quote_exact, str(item.get("keep_or_drop")))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        keep_or_drop = str(item.get("keep_or_drop", "uncertain")).strip().lower()
        if keep_or_drop not in {"keep", "drop", "uncertain"}:
            keep_or_drop = "uncertain"
        knowledge_scope = (
            str(item.get("knowledge_scope", "ambiguous")).strip().lower() or "ambiguous"
        )
        if knowledge_scope not in {
            "reusable_domain_knowledge",
            "paper_specific_evidence",
            "method_or_setup",
            "background_or_review_meta",
            "ambiguous",
        }:
            knowledge_scope = "ambiguous"
        teacher_ready = _stringify_bool(item.get("teacher_final_claim_ready"))
        teacher_reject_scope = (
            str(item.get("teacher_reject_scope", "")).strip().lower() or "none"
        )
        if teacher_reject_scope not in {
            "paper_specific_evidence",
            "method_or_setup",
            "background_or_review_meta",
            "ambiguous",
            "none",
        }:
            teacher_reject_scope = "ambiguous"
        if teacher_ready != "true" and teacher_reject_scope == "none":
            teacher_reject_scope = (
                knowledge_scope
                if knowledge_scope != "reusable_domain_knowledge"
                else "ambiguous"
            )
        if teacher_ready == "true":
            teacher_reject_scope = "none"
        entity_project_fields = _coerce_entity_project_filter_fields(
            item,
            keep_or_drop=keep_or_drop,
            fallback_reason=_normalize_claim_text(
                str(item.get("filter_reasoning", ""))
            ).rstrip(".")
            or str(item.get("drop_reason", "")).strip(),
        )
        parsed.append(
            {
                "raw_claim_text": raw_claim_text,
                "extracted_claim_text": raw_claim_text,
                "source_quote_exact": source_quote_exact,
                "citation_numbers": _coerce_citation_numbers(
                    item.get("citation_numbers")
                ),
                "citation_evidence_span": _normalize_claim_text(
                    str(item.get("citation_evidence_span", ""))
                )
                or source_quote_exact,
                "keep_or_drop": keep_or_drop,
                "drop_reason": str(item.get("drop_reason", "")).strip(),
                "claim_kind": str(item.get("claim_kind", "other")).strip().lower()
                or "other",
                "knowledge_scope": knowledge_scope,
                "knowledge_scope_reason": _normalize_claim_text(
                    str(item.get("knowledge_scope_reason", ""))
                ).rstrip("."),
                "source_anchor_valid": _stringify_bool(item.get("source_anchor_valid")),
                "filter_reasoning": _normalize_claim_text(
                    str(item.get("filter_reasoning", ""))
                ).rstrip("."),
                **entity_project_fields,
                "teacher_final_claim_ready": teacher_ready,
                "teacher_reject_scope": teacher_reject_scope,
                "teacher_review_reason": _normalize_claim_text(
                    str(item.get("teacher_review_reason", ""))
                ).rstrip("."),
            }
        )
        if max_claims and max_claims > 0 and len(parsed) >= max_claims:
            break
    return parsed


def _parse_json_or_response_payload(content: str) -> dict[str, object]:
    # strip markdown code fences if present
    stripped = re.sub(r"^```(?:json)?\s*", "", content.strip(), flags=re.I)
    stripped = re.sub(r"\s*```$", "", stripped)
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        if "claims" in payload:
            return payload
        extracted_text = _extract_response_text(payload)
        if extracted_text:
            return _parse_json_or_response_payload(extracted_text)
        nested = _find_claim_dict(payload)
        if nested:
            return nested
    match = re.search(r"\{.*\}", stripped, re.S)
    if match:
        try:
            nested_payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            partial_payload = _parse_partial_claim_payload(stripped)
            return partial_payload or {}
        if isinstance(nested_payload, dict):
            return nested_payload
    partial_payload = _parse_partial_claim_payload(stripped)
    return partial_payload or {}


def _parse_partial_claim_payload(content: str) -> dict[str, object]:
    claims_key_index = content.find('"claims"')
    if claims_key_index < 0:
        return {}
    list_start = content.find("[", claims_key_index)
    if list_start < 0:
        return {}

    claims: list[dict[str, object]] = []
    depth = 0
    object_start: int | None = None
    in_string = False
    escaped = False

    for index in range(list_start + 1, len(content)):
        char = content[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            if depth == 0:
                object_start = index
            depth += 1
            continue
        if char == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and object_start is not None:
                snippet = content[object_start : index + 1]
                try:
                    item = json.loads(snippet)
                except json.JSONDecodeError:
                    object_start = None
                    continue
                if isinstance(item, dict):
                    claims.append(item)
                object_start = None
            continue
        if char == "]" and depth == 0:
            break

    if object_start is not None and not claims:
        salvaged = _salvage_partial_claim_object(content[object_start:])
        if salvaged:
            claims.append(salvaged)

    return {"claims": claims} if claims else {}


def _salvage_partial_claim_object(content: str) -> dict[str, object]:
    raw_claim_text_match = re.search(
        r'"raw_claim_text"\s*:\s*"(?P<value>(?:[^"\\]|\\.)*)"', content
    )
    if not raw_claim_text_match:
        return {}

    raw_claim_text = _normalize_claim_text(
        _decode_json_string(raw_claim_text_match.group("value"))
    )
    if not raw_claim_text:
        return {}
    return {
        "raw_claim_text": raw_claim_text,
    }


def _find_claim_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        claims = value.get("claims")
        if isinstance(claims, list):
            return value
        for nested_value in value.values():
            nested = _find_claim_dict(nested_value)
            if nested:
                return nested
    if isinstance(value, list):
        for item in value:
            nested = _find_claim_dict(item)
            if nested:
                return nested
    return {}


def _coerce_entity_project_filter_fields(
    value: dict[str, object],
    *,
    keep_or_drop: str,
    fallback_reason: str,
) -> dict[str, str]:
    # Boolean decisions are explicit judgments, not missing/uncertain values.
    # Never infer this decision from another field such as keep_or_drop.
    raw_project = value.get("keep_for_entity_project")
    if raw_project is True:
        keep_project = "keep"
    elif raw_project is False:
        keep_project = "drop"
    elif isinstance(raw_project, str):
        normalized_project = raw_project.strip().lower()
        keep_project = {"true": "keep", "false": "drop"}.get(
            normalized_project, normalized_project
        )
        if keep_project not in {"keep", "drop", "uncertain"}:
            keep_project = "uncertain"
    else:
        keep_project = "uncertain"
    return {
        "entity_relation_summary": re.sub(
            r"\s+", " ", str(value.get("entity_relation_summary", "")).strip()
        ),
        "entity_role_freeform": re.sub(
            r"\s+", " ", str(value.get("entity_role_freeform", "")).strip()
        ),
        "keep_for_entity_project": keep_project,
        "keep_reason": re.sub(r"\s+", " ", str(value.get("keep_reason", "")).strip())
        or fallback_reason,
    }


def _stringify_bool(value: object) -> str:
    if value is True or isinstance(value, str) and value.strip().lower() == "true":
        return "true"
    if value is False or isinstance(value, str) and value.strip().lower() == "false":
        return "false"
    return "unknown"


def _parse_cluster_candidate_judge_payload(
    content: str, *, expected_ids: list[str]
) -> list[dict[str, str]]:
    payload = _parse_json_or_response_payload(content)
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        return []
    expected_set = {candidate_id for candidate_id in expected_ids if candidate_id}
    parsed: list[dict[str, str]] = []
    seen: set[object] = set()
    for item in decisions:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("raw_claim_id", "")).strip()
        if (
            not candidate_id
            or (expected_set and candidate_id not in expected_set)
            or candidate_id in seen
        ):
            continue
        seen.add(candidate_id)
        parsed.append(
            {
                "raw_claim_id": candidate_id,
                "cluster_ready": _stringify_bool(item.get("cluster_ready")),
                "decision_reason": _normalize_claim_text(
                    str(item.get("decision_reason", ""))
                ).rstrip("."),
            }
        )
    return parsed


def _parse_consensus_cluster_payload(
    content: str, *, valid_ids: set[str]
) -> list[dict[str, object]]:
    payload = _parse_json_or_response_payload(content)
    clusters = payload.get("clusters")
    if not isinstance(clusters, list):
        return []
    parsed: list[dict[str, object]] = []
    used_keys: set[tuple[str, ...]] = set()
    for item in clusters:
        if not isinstance(item, dict):
            continue
        consensus_claim = _normalize_claim_text(
            str(item.get("consensus_claim", ""))
        ).rstrip(".")
        member_ids_raw = item.get("member_raw_claim_ids", [])
        if isinstance(member_ids_raw, str):
            member_ids = [
                part.strip()
                for part in re.split(r"[,;|]", member_ids_raw)
                if part.strip()
            ]
        elif isinstance(member_ids_raw, list):
            member_ids = [
                str(part).strip() for part in member_ids_raw if str(part).strip()
            ]
        else:
            member_ids = []
        member_ids = [
            candidate_id for candidate_id in member_ids if candidate_id in valid_ids
        ]
        member_ids = list(dict.fromkeys(member_ids))
        if not consensus_claim or not member_ids:
            continue
        key = (consensus_claim, *sorted(member_ids))
        if key in used_keys:
            continue
        used_keys.add(key)
        parsed.append(
            {
                "consensus_claim": consensus_claim,
                "member_raw_claim_ids": member_ids,
                "cluster_reason": _normalize_claim_text(
                    str(item.get("cluster_reason", ""))
                ).rstrip("."),
                "mechanism_family": _normalize_claim_text(
                    str(item.get("mechanism_family", ""))
                ).rstrip("."),
                "mechanism_role": _normalize_claim_text(
                    str(item.get("mechanism_role", ""))
                ).rstrip("."),
                "specific_system": _normalize_claim_text(
                    str(item.get("specific_system", ""))
                ).rstrip("."),
                "overlap_type": _normalize_claim_text(
                    str(item.get("overlap_type", ""))
                ).rstrip("."),
                "parent_claim_hint": _normalize_claim_text(
                    str(item.get("parent_claim_hint", ""))
                ).rstrip("."),
            }
        )
    return parsed


def _parse_consensus_cluster_verifier_payload(
    content: str, *, expected_ids: list[str]
) -> list[dict[str, str]]:
    payload = _parse_json_or_response_payload(content)
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        return []
    expected_set = {claim_id for claim_id in expected_ids if claim_id}
    parsed: list[dict[str, str]] = []
    seen: set[object] = set()
    for item in decisions:
        if not isinstance(item, dict):
            continue
        claim_id = str(item.get("normalized_claim_id", "")).strip()
        if (
            not claim_id
            or (expected_set and claim_id not in expected_set)
            or claim_id in seen
        ):
            continue
        seen.add(claim_id)
        parsed.append(
            {
                "normalized_claim_id": claim_id,
                "final_consensus_ready": _stringify_bool(
                    item.get("final_consensus_ready")
                ),
                "verified_claim": (
                    _normalize_claim_text(str(item.get("verified_claim", "")))
                    if str(item.get("verified_claim", "")).strip()
                    else ""
                ),
                "decision_reason": _normalize_claim_text(
                    str(item.get("decision_reason", ""))
                ).rstrip("."),
            }
        )
    return parsed


def _normalize_claim_text(value: str) -> str:
    text = re.sub(r"\s+", " ", value.strip())
    text = text.strip("\"'`")
    if not text:
        return ""
    if text[-1] not in ".!?":
        text += "."
    return text


def _decode_json_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value


def _coerce_string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _default_impact_checks() -> dict[str, bool]:
    return {
        "changes_mechanism": False,
        "changes_design_rule": False,
        "changes_selectivity_explanation": False,
        "changes_key_experiment_or_control": False,
        "changes_screening_route": False,
        "only_local_discussion": False,
        "only_wording_level": True,
    }


def _default_evidence_checks(
    *, default_assumption_only: bool = False
) -> dict[str, bool]:
    return {
        "direct_evidence_chain": False,
        "partial_controls": False,
        "indirect_inference": not default_assumption_only,
        "default_assumption_only": default_assumption_only,
    }
