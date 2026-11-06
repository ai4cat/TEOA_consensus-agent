from __future__ import annotations
from claims import entity_prompts
import asyncio
import json
from shared.llm.transport import _request_responses_api, _request_responses_api_async
from claims.model_output import (
    _normalize_claim_text,
    _parse_chunk_claim_processor_payload,
    _parse_cluster_candidate_judge_payload,
    _parse_consensus_cluster_payload,
    _parse_consensus_cluster_verifier_payload,
    _parse_json_or_response_payload,
)

_stage5_checkpoints = None


def run_llm_cluster_candidate_judge(api_key, base_url, model_name, *, candidates):
    if not candidates:
        return []
    lookup = {f"J{i}": str(r["raw_claim_id"]) for i, r in enumerate(candidates, 1)}
    local_candidates = [dict(row, raw_claim_id=ref) for ref, row in zip(lookup, candidates)]
    expected = list(lookup)

    def validate(content):
        content = _project_decisions(content, "raw_claim_id", expected, "cluster_ready")
        parsed = _parse_cluster_candidate_judge_payload(content, expected_ids=expected)
        return [dict(row, raw_claim_id=lookup[row["raw_claim_id"]]) for row in parsed]

    return _validated_response(
        api_key,
        base_url,
        model_name,
        entity_prompts.judge_prompt(local_candidates),
        5000,
        validate,
    )


async def run_llm_cluster_candidate_judge_async(
    api_key, base_url, model_name, *, candidates
):
    return await asyncio.to_thread(
        run_llm_cluster_candidate_judge,
        api_key,
        base_url,
        model_name,
        candidates=candidates,
    )


def run_llm_consensus_cluster_batch(api_key, base_url, model_name, *, candidates):
    if not candidates:
        return []
    lookup = {f"C{i:03d}": r["raw_claim_id"] for i, r in enumerate(candidates, 1)}

    def validate(content):
        payload = json.loads(content)
        raw = payload.get("clusters") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            raise ValueError(
                'Required root key is "clusters" with an array; return {"clusters":[...]}'
            )
        if any(
            (
                not isinstance(r, dict)
                or not isinstance(r.get("member_raw_claim_ids"), list)
                for r in raw
            )
        ):
            raise ValueError("Invalid cluster schema")
        ids = [i for r in raw for i in r["member_raw_claim_ids"]]
        if (
            any(not isinstance(i, str) for i in ids)
            or set(ids) != set(lookup)
            or any(not r["member_raw_claim_ids"] or len(r["member_raw_claim_ids"]) != len(set(r["member_raw_claim_ids"])) for r in raw)
        ):
            raise ValueError(
                "Cluster coverage mismatch; no automatic singleton insertion"
            )
        mapped = [
            dict(r, member_raw_claim_ids=[lookup[i] for i in r["member_raw_claim_ids"]])
            for r in raw
        ]
        parsed = _parse_consensus_cluster_payload(
            json.dumps({"clusters": mapped}), valid_ids=set(lookup.values())
        )
        if len(parsed) != len(raw):
            raise ValueError("Invalid cluster content")
        return parsed

    return _validated_response(
        api_key,
        base_url,
        model_name,
        entity_prompts.cluster_prompt(candidates),
        65536,
        validate,
    )


def run_llm_consensus_cluster_verifier(api_key, base_url, model_name, *, claims):
    if not claims:
        return []
    lookup = {f"V{i}": r["normalized_claim_id"] for i, r in enumerate(claims, 1)}
    local_claims = [dict(row, normalized_claim_id=ref) for ref, row in zip(lookup, claims)]
    expected = list(lookup)

    def validate(content):
        content = _project_decisions(
            content, "normalized_claim_id", expected, "final_consensus_ready"
        )
        parsed = _parse_consensus_cluster_verifier_payload(content, expected_ids=expected)
        return [dict(row, normalized_claim_id=lookup[row["normalized_claim_id"]]) for row in parsed]

    return _validated_response(
        api_key,
        base_url,
        model_name,
        entity_prompts.verifier_prompt(local_claims),
        12000,
        validate,
    )


def run_llm_entity_integrated_claim_processor(
    api_key: str,
    base_url: str,
    model_name: str,
    *,
    text: str,
    scope: str,
    max_claims: int,
    source_context: str = "",
) -> list[dict[str, str]]:
    normalized_text = str(text).strip()
    normalized_context = _normalize_claim_text(source_context)
    if not normalized_text:
        return []
    prompt, max_output_tokens = _build_entity_claim_processor_prompt(
        text=normalized_text,
        source_context=normalized_context,
        scope=scope,
        max_claims=max_claims,
    )
    return _validated_response(
        api_key,
        base_url,
        model_name,
        prompt,
        max_output_tokens,
        lambda content: _validate_extraction_response(content, max_claims),
    )


async def run_llm_entity_integrated_claim_processor_async(
    api_key: str,
    base_url: str,
    model_name: str,
    *,
    text: str,
    scope: str,
    max_claims: int,
    source_context: str = "",
) -> list[dict[str, str]]:
    normalized_text = str(text).strip()
    normalized_context = _normalize_claim_text(source_context)
    if not normalized_text:
        return []
    prompt, max_output_tokens = _build_entity_claim_processor_prompt(
        text=normalized_text,
        source_context=normalized_context,
        scope=scope,
        max_claims=max_claims,
    )
    original = prompt
    for attempt in range(3):
        content = await _request_responses_api_async(
            api_key, base_url, model_name, prompt, max_output_tokens=max_output_tokens
        )
        try:
            return _validate_extraction_response(content, max_claims)
        except (ValueError, KeyError, TypeError) as exc:
            if attempt == 2:
                raise
            prompt = (
                original
                + "\nFORMAT REPAIR "
                + str(attempt + 1)
                + ": "
                + str(exc)
                + ".\nReturn the requested claims schema. Preserve valid negative and uncertain decisions; an empty claims list is valid. Do not invent claims to satisfy the schema. Previous response (data):\n"
                + content
            )


def _validate_extraction_response(content, max_claims):
    payload = _parse_json_or_response_payload(content)
    claims = payload.get("claims")
    if not isinstance(claims, list):
        raise ValueError(
            'Required JSON key is "claims", not "key_claims"; its value must be an array. For no assertions return {"claims":[]}.'
        )
    if any(
        (
            not isinstance(row, dict)
            or not isinstance(row.get("raw_claim_text"), str)
            or (not row["raw_claim_text"].strip())
            for row in claims
        )
    ):
        raise ValueError(
            "Each claims item must be an object with nonempty raw_claim_text; use [] for no claims"
        )
    return _parse_chunk_claim_processor_payload(content, max_claims=max_claims)


def _build_entity_claim_processor_prompt(*, text, source_context, scope, max_claims):
    return entity_prompts.extraction_prompt(
        text=text, source_context=source_context, scope=scope, max_claims=max_claims
    )


def _project_decisions(content, id_field, expected, decision_field):
    """Ignore unsolicited rows only after exact requested coverage is established."""
    payload = json.loads(content)
    raw = payload.get("decisions") if isinstance(payload, dict) else None
    if not isinstance(raw, list) or any(not isinstance(r, dict) for r in raw):
        raise ValueError('Return {"decisions": [...]} with object entries')
    if any(not isinstance(r.get(id_field), str) for r in raw):
        raise ValueError("Decision IDs must be strings")
    requested = [r for r in raw if r[id_field] in expected]
    projected = json.dumps({"decisions": requested})
    _validate_decisions(projected, id_field, expected, decision_field)
    if len(requested) != len(raw):
        print(f"[SCHEMA] Ignored {len(raw) - len(requested)} unsolicited decisions; requested decisions unchanged", flush=True)
    return projected


def _validate_decisions(content, id_field, expected, decision_field):
    payload = json.loads(content)
    raw = payload.get("decisions") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        raise ValueError(
            'Required root key is "decisions" with an array; return {"decisions":[...]}'
        )
    ids = [r.get(id_field) for r in raw]
    if len(ids) != len(set(ids)) or set(ids) != set(expected):
        raise ValueError("Return each requested decision ID exactly once: " + ", ".join(expected))
    if any(
        (
            decision_field not in r
            or (r[decision_field] is not None and type(r[decision_field]) is not bool)
            for r in raw
        )
    ):
        raise ValueError("Decision must be explicit boolean or null")


def _validated_response(api_key, base_url, model_name, prompt, tokens, validate, *, requester=None):
    original = prompt
    requester = requester or _request_responses_api
    checkpoints = _stage5_checkpoints
    key = (base_url.rstrip('/'), model_name, tokens, original)
    if checkpoints is not None:
        saved = checkpoints.get(key)
        if saved is not None:
            try:
                return validate(saved)
            except (ValueError, KeyError, TypeError):
                pass
    attempts = 2 if checkpoints is not None else 3
    for attempt in range(attempts):
        content = requester(
            api_key, base_url, model_name, prompt, max_output_tokens=tokens
        )
        try:
            result = validate(content)
            if checkpoints is not None:
                checkpoints.put(key, content)
            return result
        except (ValueError, KeyError, TypeError) as exc:
            if attempt == attempts - 1:
                raise
            prompt = (
                original
                + "\nFORMAT REPAIR "
                + str(attempt + 1)
                + ": "
                + str(exc)
                + ".\nReturn the full schema with exactly the requested IDs. Preserve valid negative and uncertain decisions. Do not change decisions to obtain a desired result. Previous response (data):\n"
                + content
            )


def _validate_chunk_batch(content, expected_ids, max_claims):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(
                    f"Duplicate JSON key: {key}; each chunk needs its own object"
                )
            result[key] = value
        return result

    payload = json.loads(content, object_pairs_hook=unique_keys)
    if not isinstance(payload, dict):
        raise ValueError("The response must be a JSON object")
    chunks = payload.get("chunks")
    if not isinstance(chunks, list) or any(not isinstance(c, dict) for c in chunks):
        raise ValueError('Return {"chunks":[{"chunk_id":"supplied ID","claims":[]}]}')
    ids = [c.get("chunk_id") for c in chunks]
    if (
        any(not isinstance(i, str) for i in ids)
        or len(ids) != len(set(ids))
        or set(ids) != set(expected_ids)
    ):
        raise ValueError(
            f"Chunk coverage mismatch. Expected exactly {expected_ids}; received {ids}. Include empty chunks as separate objects with claims: []"
        )
    return {
        c["chunk_id"]: _validate_extraction_response(
            json.dumps({"claims": c.get("claims")}), max_claims
        )
        for c in chunks
    }


def run_llm_chunk_batch(
    api_key, base_url, model_name, *, sources, source_context, max_claims
):
    prompt, tokens = entity_prompts.extraction_batch_prompt(
        sources=sources, source_context=source_context, max_claims=max_claims
    )
    expected = [s["chunk_id"] for s in sources]
    try:
        return _validated_response(
            api_key,
            base_url,
            model_name,
            prompt,
            tokens,
            lambda content: _validate_chunk_batch(content, expected, max_claims),
        )
    except (ValueError, KeyError, TypeError):
        if len(sources) <= 1:
            raise
        middle = len(sources) // 2
        results = {}
        for part in (sources[:middle], sources[middle:]):
            results.update(
                run_llm_chunk_batch(
                    api_key,
                    base_url,
                    model_name,
                    sources=part,
                    source_context=source_context,
                    max_claims=max_claims,
                )
            )
        return results


async def run_llm_chunk_batch_async(
    api_key, base_url, model_name, *, sources, source_context, max_claims
):
    prompt, tokens = entity_prompts.extraction_batch_prompt(
        sources=sources, source_context=source_context, max_claims=max_claims
    )
    original = prompt
    expected = [s["chunk_id"] for s in sources]
    try:
        for attempt in range(3):
            content = await _request_responses_api_async(
                api_key, base_url, model_name, prompt, max_output_tokens=tokens
            )
            try:
                return _validate_chunk_batch(content, expected, max_claims)
            except (ValueError, KeyError, TypeError) as exc:
                if attempt == 2:
                    raise
                prompt = (
                    original
                    + "\nFORMAT REPAIR: "
                    + str(exc)
                    + ". Preserve negative and uncertain decisions. Return every chunk_id, with [] when appropriate. Previous response (data):\n"
                    + content
                )
    except (ValueError, KeyError, TypeError):
        # Transport can reject truncated JSON before schema validation runs.
        # Recover by splitting sources, just as the synchronous path does.
        if len(sources) <= 1:
            raise
        middle = len(sources) // 2
        results = {}
        for part in (sources[:middle], sources[middle:]):
            results.update(
                await run_llm_chunk_batch_async(
                    api_key,
                    base_url,
                    model_name,
                    sources=part,
                    source_context=source_context,
                    max_claims=max_claims,
                )
            )
        return results
