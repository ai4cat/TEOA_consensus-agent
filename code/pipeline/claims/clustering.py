from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from shared.llm.transport import _request_responses_api
from shared.settings import PipelineConfig
from shared.storage import read_csv, write_csv
from claims.claim_ids import new_claim_id
from claims.extraction import (
    CONSENSUS_CLAIM,
    MANUAL_SEED_CLAIM,
    discover_raw_claim_candidates,
)
from claims.model_tasks import (
    run_llm_cluster_candidate_judge,
    run_llm_cluster_candidate_judge_async,
    run_llm_consensus_cluster_batch,
    run_llm_consensus_cluster_verifier,
)
from claims.provenance import (
    _attach_source_index_chunks,
    _build_origin_occurrence_rows,
    _parse_origin_evidence_items,
)
from scoring.scoring.audit import require_llm_env

CONSENSUS_CLAIM_MASTER_FIELDS = [
    "consensus_claim_id",
    "consensus_claim_text",
    "source_generalized_claim_ids",
    "source_normalized_claim_ids",
    "source_raw_claim_ids",
    "source_chunk_ids",
    "origin_doc_ids",
    "origin_evidence_json",
    "reference_used",
    "claim_status",
    "claim_status_reason",
    "generalized_claim_count",
    "normalized_claim_count",
    "raw_claim_count",
    "df",
    "wdf",
    "total_doc_count",
    "saturation_score",
    "top_citing_docs",
    "supporting_doc_ids",
    "consensus_rank",
    "frequency_evidence",
]

CONSENSUS_CLAIM_SUMMARY_FIELDS = [
    "consensus_claim_id",
    "consensus_claim_text",
    "source_generalized_claim_ids",
    "source_normalized_claim_ids",
    "source_raw_claim_ids",
    "source_chunk_ids",
    "origin_doc_ids",
    "reference_used",
    "claim_status",
    "claim_status_reason",
    "generalized_claim_count",
    "normalized_claim_count",
    "raw_claim_count",
    "df",
    "wdf",
    "total_doc_count",
    "saturation_score",
    "top_citing_docs",
    "supporting_doc_ids",
    "consensus_rank",
]

CONSENSUS_HYGIENE_REVIEW_FIELDS = [
    "consensus_claim_id",
    "consensus_claim_text",
    "df",
    "raw_claim_count",
    "entity_focus",
    "decision",
    "decision_reason",
]

STEP3B_CLUSTER_QUALITY_AUDIT_FIELDS = [
    "normalized_claim_id",
    "normalized_claim_text",
    "mechanism_family",
    "mechanism_role",
    "specific_system",
    "overlap_type",
    "parent_claim_hint",
    "raw_claim_count",
    "origin_doc_ids",
    "origin_chunk_ids",
    "member_raw_claim_ids",
    "sample_contexts",
    "normalization_reason",
]

CONSENSUS_CLAIM_NOTE = (
    "Normalized claim means a Step 3B deduplicated raw-claim group; it does not imply broad multi-document "
    "agreement unless df/raw support is high."
)


CONSENSUS_DIRECT_SOURCE_FIELDS = (
    "source_chunk_id",
    "source_doc_id",
    "source_page_number",
    "source_paragraph_id",
    "source_context",
    "source_chunk_status",
    "source_chunk_selection_reason",
)

RAW_CANDIDATE_FIELD_ORDER = [
    "raw_claim_id",
    "raw_claim_text",
    "extracted_claim_text",
    "source_quote_exact",
    "doc_id",
    "chunk_id",
    "page_number",
    "paragraph_id",
    "citation_numbers",
    "reference_used",
    "evidence_scheme_json",
    "source_type",
    "source_name",
    "claim_type",
    "keep_or_drop",
    "claim_kind",
    "knowledge_scope",
    "source_anchor_valid",
    "entity_relation_summary",
    "entity_role_freeform",
    "keep_for_entity_project",
    "teacher_final_claim_ready",
    "teacher_reject_scope",
    "guardrail_final_claim_ready",
    "project_focus_ready",
    "stage3a_accepted",
    "anchor_sentence_text",
    "next_sentence_text",
    "review_context_3sent",
]

STRUCTURED_POOL_STRIP_FIELDS = {
    "claim_zh",
    "source_types",
    "source_names",
    "variants",
    "manual_seed_count",
    "auto_candidate_count",
    "claim_type",
    "claim_type_reason",
    "normalization_reason",
    "claim_type_counts",
    "mention_density",
    "mention_density_mean",
}


@dataclass(slots=True)
class Step3RunContext:
    config: PipelineConfig
    seed_claims: list[dict[str, str]]
    chunk_rows: list[dict[str, str]]
    doc_rows: list[dict[str, str]]
    doc_type_by_doc_id: dict[str, str]
    total_doc_count: int


def _run_step3a(
    context: Step3RunContext,
    *,
    reuse_raw_candidates: bool,
) -> list[dict[str, object]]:
    config = context.config
    if reuse_raw_candidates:
        if not config.raw_claim_candidates_path.exists():
            raise FileNotFoundError(
                f"Requested raw-candidate reuse, but cache is missing: {config.raw_claim_candidates_path.name}"
            )
        print(
            f"[Stage 4] Reusing cached raw candidates from {config.raw_claim_candidates_path.name}"
        )
        raw_candidates = read_csv(config.raw_claim_candidates_path)
        print(f"[OK] Reused {len(raw_candidates)} raw candidates")
    else:
        print("[Stage 4] Discovering raw claim candidates...")
        raw_candidates = discover_raw_claim_candidates(
            config,
            context.seed_claims,
            context.chunk_rows,
            context.doc_rows,
            require_llm_env("STEP3A"),
        )
        print(f"[OK] Discovered {len(raw_candidates)} raw candidates")

    raw_candidates = _drop_manual_seed_raw_candidates(
        raw_candidates, use_seed_claims=bool(context.seed_claims)
    )
    _write_rows(config.raw_claim_candidates_path, raw_candidates)
    _write_claim_scope_review_artifacts(config, raw_candidates)
    print(f"[OK] Wrote {config.raw_claim_candidates_path.name}")
    return raw_candidates


def _drop_manual_seed_raw_candidates(
    raw_candidates: list[dict[str, object]],
    *,
    use_seed_claims: bool,
) -> list[dict[str, object]]:
    if use_seed_claims:
        return raw_candidates
    return [
        row
        for row in raw_candidates
        if str(row.get("source_type", "")).strip().lower() != "manual_seed"
    ]


def _run_step3b(
    context: Step3RunContext, raw_candidates: list[dict[str, object]]
) -> list[dict[str, object]]:
    return _run_step3b_llm_consensus_clustering(context, raw_candidates)


CLUSTER_CANDIDATE_REVIEW_FIELDS = [
    "source_quote_exact",
    "raw_claim_id",
    "source_type",
    "source_name",
    "doc_id",
    "chunk_id",
    "page_number",
    "raw_claim_text",
    "paragraph_id",
    "citation_numbers",
    "reference_used",
    "evidence_scheme_json",
    "claim_type",
    "prev_sentence_text",
    "next_sentence_text",
    "review_context_3sent",
    "cluster_ready",
    "decision_reason",
]

CONSENSUS_VERIFIER_REVIEW_FIELDS = [
    "normalized_claim_id",
    "normalized_claim_text",
    "final_consensus_ready",
    "verified_claim",
    "decision_reason",
    "raw_claim_count",
    "origin_doc_ids",
    "origin_chunk_ids",
]


def _run_step3b_llm_consensus_clustering(
    context: Step3RunContext,
    raw_candidates: list[dict[str, object]],
) -> list[dict[str, object]]:
    print(
        "[Stage 5] LLM judging cluster-ready raw candidates and clustering into consensus claims..."
    )
    llm_env = require_llm_env("STEP3B")
    judge_path = (
        context.config.claim_generation_dir / "03B_cluster_candidate_review.csv"
    )
    eligible = [r for r in raw_candidates if r.get("stage3a_accepted") == "true"]
    judge_rows = _judge_cluster_ready_candidates(context, eligible, llm_env)
    _write_rows(judge_path, judge_rows, CLUSTER_CANDIDATE_REVIEW_FIELDS)
    cluster_ready_rows = [
        row
        for row in judge_rows
        if str(row.get("cluster_ready", "")).strip().lower() == "true"
    ]
    print(
        f"[OK] LLM marked {len(cluster_ready_rows)}/{len(judge_rows)} candidates as cluster-ready"
    )
    structured_claims = _cluster_ready_candidates_to_consensus_claims(
        context, cluster_ready_rows, llm_env
    )
    structured_claims = _verify_llm_consensus_claims(
        context, structured_claims, llm_env
    )
    structured_claims = _limit_llm_consensus_claim_pool(
        structured_claims,
        context.config.claim_pool_max_size,
    )
    _write_step3b_cluster_quality_audit(context, structured_claims)
    structured_claims = _clear_consensus_direct_source_fields(structured_claims)
    structured_claims = _strip_structured_pool_display_fields(structured_claims)
    print(
        f"[OK] Retained {len(structured_claims)} LLM consensus claims in the structured pool"
    )
    _write_rows(context.config.structured_claim_pool_path, structured_claims)
    print(f"[OK] Wrote {context.config.structured_claim_pool_path.name}")
    return structured_claims


def _clear_consensus_direct_source_fields(
    claims: list[dict[str, object]],
) -> list[dict[str, object]]:
    cleared: list[dict[str, object]] = []
    for claim in claims:
        updated = dict(claim)
        for field in CONSENSUS_DIRECT_SOURCE_FIELDS:
            updated[field] = ""
        cleared.append(updated)
    return cleared


def _strip_structured_pool_display_fields(
    claims: list[dict[str, object]],
) -> list[dict[str, object]]:
    stripped: list[dict[str, object]] = []
    for claim in claims:
        updated = dict(claim)
        for field in STRUCTURED_POOL_STRIP_FIELDS:
            updated.pop(field, None)
        stripped.append(updated)
    return stripped


def _limit_llm_consensus_claim_pool(
    claims: list[dict[str, object]],
    max_size: int,
) -> list[dict[str, object]]:
    if max_size <= 0:
        return claims
    return sorted(
        claims,
        key=lambda row: (
            float(row.get("raw_claim_count", 0) or 0),
            float(row.get("wdf", 0) or 0),
            str(row.get("normalized_claim_text", "")),
        ),
        reverse=True,
    )[:max_size]


def _verify_llm_consensus_claims(
    context: Step3RunContext,
    structured_claims: list[dict[str, object]],
    llm_env: dict[str, str],
) -> list[dict[str, object]]:
    if not structured_claims:
        _write_rows(
            context.config.claim_generation_dir / "03B_consensus_verifier_review.csv",
            [],
            CONSENSUS_VERIFIER_REVIEW_FIELDS,
        )
        return []
    compact_claims = [
        {
            "normalized_claim_id": str(claim.get("normalized_claim_id", "")),
            "normalized_claim_text": str(claim.get("normalized_claim_text", "")),
            "raw_claim_count": str(claim.get("raw_claim_count", "")),
            "origin_doc_ids": str(claim.get("origin_doc_ids", "")),
            "origin_evidence_json": str(claim.get("origin_evidence_json", "")),
        }
        for claim in structured_claims
    ]
    batch_size = max(
        1, int(getattr(context.config, "step3b_llm_verifier_batch_size", 40) or 40)
    )
    batches = []
    batch = []
    size = 0
    for claim in compact_claims:
        item_size = len(json.dumps(claim, ensure_ascii=False))
        if batch and (len(batch) >= batch_size or size + item_size > 48000):
            batches.append(batch)
            batch, size = [], 0
        batch.append(claim)
        size += item_size
    if batch:
        batches.append(batch)
    decisions: list[dict[str, str]] = []
    pending_batches = list(enumerate(batches, start=1))
    if pending_batches:
        workers = max(
            1,
            min(
                int(getattr(context.config, "step3b_llm_verifier_workers", 1) or 1),
                len(pending_batches),
            ),
        )
        max_attempts = max(
            1, int(getattr(context.config, "step3b_llm_verifier_max_attempts", 3) or 3)
        )
        retry_sleep_seconds = max(
            0.0,
            float(
                getattr(context.config, "step3b_llm_verifier_retry_sleep_seconds", 8.0)
                or 8.0
            ),
        )
        print(
            f"  [VERIFIER RUN] pending_batches={len(pending_batches)} batch_size={batch_size} workers={workers}",
            flush=True,
        )
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _run_consensus_verifier_batch_with_retry,
                    batch_index,
                    batch,
                    llm_env,
                    max_attempts,
                    retry_sleep_seconds,
                ): (batch_index, batch)
                for batch_index, batch in pending_batches
            }
            for future in as_completed(futures):
                batch_index, batch = futures[future]
                batch_decisions = future.result()
                print(
                    f"  [VERIFIER LLM] batch {batch_index}/{len(batches)} claims={len(batch)} decisions={len(batch_decisions)}",
                    flush=True,
                )
                decisions.extend(batch_decisions)
    decision_by_normalized_claim_id = {
        str(row.get("normalized_claim_id", "")).strip(): row for row in decisions
    }
    review_rows: list[dict[str, object]] = []
    kept: list[dict[str, object]] = []
    for claim in structured_claims:
        normalized_claim_id = str(claim.get("normalized_claim_id", "")).strip()
        decision = decision_by_normalized_claim_id.get(normalized_claim_id, {})
        final_ready = (
            str(decision.get("final_consensus_ready", "false")).strip().lower()
        )
        verified_claim = str(decision.get("verified_claim", "")).strip()
        decision_reason = str(
            decision.get("decision_reason", "Verifier returned no decision.")
        ).strip()
        review_rows.append(
            {
                "normalized_claim_id": normalized_claim_id,
                "normalized_claim_text": claim.get("normalized_claim_text", ""),
                "final_consensus_ready": final_ready,
                "verified_claim": verified_claim,
                "decision_reason": decision_reason,
                "raw_claim_count": claim.get("raw_claim_count", ""),
                "origin_doc_ids": claim.get("origin_doc_ids", ""),
                "origin_chunk_ids": claim.get("origin_chunk_ids", ""),
            }
        )
        if final_ready != "true":
            continue
        updated_claim = dict(claim)
        if (
            verified_claim
            and verified_claim != str(claim.get("normalized_claim_text", "")).strip()
        ):
            updated_claim["normalized_claim_text"] = verified_claim
        kept.append(updated_claim)
    _write_rows(
        context.config.claim_generation_dir / "03B_consensus_verifier_review.csv",
        review_rows,
        CONSENSUS_VERIFIER_REVIEW_FIELDS,
    )
    print(
        f"[OK] Verifier retained {len(kept)}/{len(structured_claims)} consensus claims"
    )
    return kept


def _run_consensus_verifier_batch_with_retry(
    batch_index: int,
    batch: list[dict[str, str]],
    llm_env: dict[str, str],
    max_attempts: int,
    retry_sleep_seconds: float,
) -> list[dict[str, str]]:
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        started = time.time()
        try:
            decisions = run_llm_consensus_cluster_verifier(
                llm_env["LLM_API_KEY"],
                llm_env["LLM_API_BASE_URL"],
                llm_env["LLM_MODEL_NAME"],
                claims=batch,
            )
            print(
                f"  [VERIFIER OK] batch {batch_index} attempt={attempt} seconds={time.time() - started:.1f}",
                flush=True,
            )
            return decisions
        except Exception as exc:
            last_error = str(exc)
            print(
                f"  [VERIFIER RETRY] batch {batch_index} attempt={attempt} error={last_error[:240]}",
                flush=True,
            )
            if attempt < max_attempts:
                time.sleep(retry_sleep_seconds * attempt)
    raise RuntimeError(
        f"Step3B verifier batch {batch_index} failed after {max_attempts} attempts: {last_error}"
    )


def _judge_cluster_ready_candidates(
    context: Step3RunContext,
    raw_candidates: list[dict[str, object]],
    llm_env: dict[str, str],
) -> list[dict[str, object]]:
    candidates = [
        candidate
        for candidate in raw_candidates
        if str(candidate.get("raw_claim_id", "")).strip() and _raw_claim_text(candidate)
    ]
    if not candidates:
        return []
    batch_size = max(
        1, int(getattr(context.config, "step3b_llm_judge_batch_size", 30) or 30)
    )
    batches = [
        candidates[index : index + batch_size]
        for index in range(0, len(candidates), batch_size)
    ]
    decision_by_raw_claim_id: dict[str, dict[str, str]] = {}
    workers = max(
        1,
        min(
            int(getattr(context.config, "claim_discovery_chunk_workers", 8) or 8),
            len(batches),
        ),
    )

    def _judge_batch(batch: list[dict[str, object]]) -> list[dict[str, str]]:
        return run_llm_cluster_candidate_judge(
            llm_env["LLM_API_KEY"],
            llm_env["LLM_API_BASE_URL"],
            llm_env["LLM_MODEL_NAME"],
            candidates=[_cluster_candidate_payload(row) for row in batch],
        )

    step3_async_enabled = str(
        os.getenv("PIPELINE_STEP3_ASYNC", "1")
    ).strip().lower() not in {"0", "false", "no", "off"}
    if step3_async_enabled:

        async def _judge_batches_async() -> list[dict[str, str]]:
            semaphore = asyncio.Semaphore(workers)

            async def _judge_one(
                batch: list[dict[str, object]],
            ) -> list[dict[str, str]]:
                async with semaphore:
                    return await run_llm_cluster_candidate_judge_async(
                        llm_env["LLM_API_KEY"],
                        llm_env["LLM_API_BASE_URL"],
                        llm_env["LLM_MODEL_NAME"],
                        candidates=[_cluster_candidate_payload(row) for row in batch],
                    )

            gathered = await asyncio.gather(*[_judge_one(batch) for batch in batches])
            decisions: list[dict[str, str]] = []
            for batch_decisions in gathered:
                decisions.extend(batch_decisions)
            return decisions

        for decision in asyncio.run(_judge_batches_async()):
            raw_claim_id = str(decision.get("raw_claim_id", "")).strip()
            if raw_claim_id:
                decision_by_raw_claim_id[raw_claim_id] = decision
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_judge_batch, batch) for batch in batches]
            for future in as_completed(futures):
                for decision in future.result():
                    raw_claim_id = str(decision.get("raw_claim_id", "")).strip()
                    if raw_claim_id:
                        decision_by_raw_claim_id[raw_claim_id] = decision

    rows: list[dict[str, object]] = []
    for candidate in candidates:
        raw_claim_id = str(candidate.get("raw_claim_id", "")).strip()
        decision = decision_by_raw_claim_id.get(raw_claim_id, {})
        missing_decision = not decision
        cluster_ready = (
            str(decision.get("cluster_ready", "false")).strip().lower() or "false"
        )
        decision_reason = str(
            decision.get(
                "decision_reason",
                "LLM judge returned no decision for this candidate; excluded from clustering.",
            )
        ).strip()
        rows.append(
            {
                **{
                    field: candidate.get(field, "")
                    for field in CLUSTER_CANDIDATE_REVIEW_FIELDS
                    if field not in {"cluster_ready", "decision_reason"}
                },
                "cluster_ready": cluster_ready,
                "decision_reason": decision_reason,
            }
        )
        if missing_decision and cluster_ready != "true":
            print(
                f"[WARN] Missing cluster judge decision for {raw_claim_id}; excluded from clustering"
            )
    return rows


def _cluster_ready_candidates_to_consensus_claims(
    context: Step3RunContext,
    cluster_ready_rows: list[dict[str, object]],
    llm_env: dict[str, str],
) -> list[dict[str, object]]:
    if not cluster_ready_rows:
        return []
    batch_size = max(
        1, int(getattr(context.config, "step3b_llm_cluster_batch_size", 120) or 120)
    )
    batches = [
        cluster_ready_rows[index : index + batch_size]
        for index in range(0, len(cluster_ready_rows), batch_size)
    ]
    clusters: list[dict[str, object]] = []
    # Stage 1: Batch-level clustering
    print(f"  Stage 1: Batch-level clustering ({len(batches)} batches)...")
    clusters_by_batch: dict[int, list[dict[str, object]]] = {}
    pending_batches = []
    for batch_index, batch in enumerate(batches, start=1):
        print(f"    Batch {batch_index}/{len(batches)} ({len(batch)} candidates)...")
        pending_batches.append((batch_index, batch))

    def _cluster_batch(batch):
        batch_clusters = run_llm_consensus_cluster_batch(
            llm_env["LLM_API_KEY"],
            llm_env["LLM_API_BASE_URL"],
            llm_env["LLM_MODEL_NAME"],
            candidates=[_cluster_candidate_payload(row) for row in batch],
        )
        return _ensure_cluster_candidate_coverage(batch_clusters, batch)

    if pending_batches:
        workers = max(
            1,
            min(
                int(getattr(context.config, "claim_discovery_chunk_workers", 8) or 8),
                len(pending_batches),
            ),
        )
        errors = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_cluster_batch, batch): batch_index
                for batch_index, batch in pending_batches
            }
            for future in as_completed(futures):
                batch_index = futures[future]
                try:
                    batch_clusters = future.result()
                except Exception as exc:
                    errors.append((batch_index, exc))
                    continue
                clusters_by_batch[batch_index] = batch_clusters
                # Flatten in batch order below so completion order cannot
                # change the cross-batch prompt or final claim identities.
        if errors:
            batch_index, error = sorted(errors, key=lambda item: item[0])[0]
            raise RuntimeError(
                f"Step3B clustering batch {batch_index} failed"
            ) from error
    for batch_index in range(1, len(batches) + 1):
        clusters.extend(clusters_by_batch[batch_index])

    # Stage 2: Cross-batch merging (if multiple batches)
    if len(batches) > 1:
        print(
            f"  Stage 2: Cross-batch merging ({len(clusters)} clusters from {len(batches)} batches)..."
        )
        clusters = _merge_cross_batch_clusters(
            clusters, llm_env,
            candidates_by_id={str(row["raw_claim_id"]): row for row in cluster_ready_rows},
        )

    return _consensus_clusters_to_structured_claims(
        clusters,
        candidates_by_id={
            str(row.get("raw_claim_id", "")).strip(): row for row in cluster_ready_rows
        },
        doc_type_by_doc_id=context.doc_type_by_doc_id,
        config=context.config,
    )


def _build_cross_batch_merge_prompt(cluster_summaries):
    from claims.entity_prompts import merge_prompt

    return merge_prompt(cluster_summaries)


def _merge_cross_batch_clusters(
    batch_clusters: list[dict[str, object]],
    llm_env: dict[str, str],
    *,
    candidates_by_id: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    """Merge validated, disjoint cluster groups; provider failure stops the stage."""
    if len(batch_clusters) <= 1:
        return batch_clusters
    member_ids = sorted({rid for cluster in batch_clusters for rid in cluster["member_raw_claim_ids"]})
    evidence_refs = {rid: f"E{i}" for i, rid in enumerate(member_ids, 1)}
    summaries = [
        {
            "cluster_id": f"G{i + 1}",
            "consensus_claim": str(cluster.get("consensus_claim", "")).strip(),
            "mechanism_family": str(cluster.get("mechanism_family", "")).strip(),
            "member_count": len(cluster.get("member_raw_claim_ids", [])),
            "member_evidence_refs": [evidence_refs[rid] for rid in cluster["member_raw_claim_ids"]],
            "cluster_reason": str(cluster.get("cluster_reason", ""))[:200],
        }
        for i, cluster in enumerate(batch_clusters)
    ]
    source_assertions = {
        evidence_refs[rid]: {
            "claim_text": str(candidates_by_id[rid]["raw_claim_text"]),
            "source_quote_exact": str(candidates_by_id[rid].get("source_quote_exact", "")),
        }
        for rid in member_ids
    }
    prompt = _build_cross_batch_merge_prompt(
        {"clusters": summaries, "source_assertions": source_assertions}
    )
    id_to_index = {f"G{i + 1}": i for i in range(len(batch_clusters))}
    from claims.model_tasks import _validated_response

    def merge_request(*args, **kwargs):
        # Cross-batch evidence can exceed 600k characters. Keep its prompt and
        # checkpoint identity unchanged; allow time for provider prefill/reasoning.
        return _request_responses_api(
            *args, **kwargs, read_timeout_seconds=600, response_deadline_seconds=1800
        )

    groups = _validated_response(
        llm_env["LLM_API_KEY"], llm_env["LLM_API_BASE_URL"], llm_env["LLM_MODEL_NAME"],
        prompt, 65536, lambda content: _validate_cross_batch_merge(content, id_to_index),
        requester=merge_request,
    )
    # Validate the entire response before creating any merged output.
    final = [
        _merge_cluster_group([batch_clusters[i] for i in group], representative=representative)
        for group, representative in groups
    ]
    used = {i for group, _ in groups for i in group}
    final.extend(cluster for i, cluster in enumerate(batch_clusters) if i not in used)
    print(f"    Merged {len(batch_clusters)} -> {len(final)} clusters")
    return final


def _validate_cross_batch_merge(content, id_to_index):
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid cross-batch merge JSON; stage not completed") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("merge_groups"), list):
        raise ValueError("Cross-batch merge requires a merge_groups array")
    seen = set()
    groups = []
    for proposal in parsed["merge_groups"]:
        if not isinstance(proposal, dict):
            raise ValueError("Each merge proposal requires cluster_ids and consensus_claim")
        group = proposal.get("cluster_ids")
        representative = proposal.get("consensus_claim")
        if not isinstance(representative, str) or not representative.strip():
            raise ValueError("Merged representative must be supplied by the model")
        if not isinstance(group, list) or len(group) < 2:
            raise ValueError("Each merge group must contain at least two cluster IDs")
        if any(not isinstance(cid, str) or cid not in id_to_index for cid in group):
            raise ValueError("Merge IDs must be selected from " + ", ".join(id_to_index) + "; E references are evidence, not groups")
        if len(set(group)) != len(group) or seen.intersection(group):
            raise ValueError(
                "Merge groups must be disjoint and contain no duplicate IDs"
            )
        seen.update(group)
        groups.append(([id_to_index[cid] for cid in group], representative.strip()))
    return groups


def _merge_cluster_group(clusters: list[dict[str, object]], *, representative: str) -> dict[str, object]:
    """Merge multiple clusters into one."""
    if not clusters:
        return {}

    # Combine member_raw_claim_ids
    all_member_ids = []
    for cluster in clusters:
        all_member_ids.extend(cluster.get("member_raw_claim_ids", []))
    deduped_member_ids = list(
        dict.fromkeys(
            str(member_id).strip()
            for member_id in all_member_ids
            if str(member_id).strip()
        )
    )

    merged_consensus = representative.strip()

    # Use the first mechanism_family
    mechanism_family = str(clusters[0].get("mechanism_family", ""))

    # Combine cluster_reasons
    reasons = [str(c.get("cluster_reason", ""))[:100] for c in clusters]
    merged_reason = f"Cross-batch merge of {len(clusters)} clusters: " + "; ".join(
        reasons
    )

    return {
        "consensus_claim": merged_consensus,
        "member_raw_claim_ids": deduped_member_ids,
        "cluster_reason": merged_reason,
        "mechanism_family": mechanism_family,
        "mechanism_role": clusters[0].get("mechanism_role", ""),
        "specific_system": clusters[0].get("specific_system", ""),
        "overlap_type": "cross_batch_merge",
        "parent_claim_hint": clusters[0].get("parent_claim_hint", ""),
    }


def _ensure_cluster_candidate_coverage(clusters, candidates):
    expected = {r["raw_claim_id"] for r in candidates}
    ids = [i for c in clusters for i in c["member_raw_claim_ids"]]
    if set(ids) != expected or any(
        not c["member_raw_claim_ids"]
        or len(c["member_raw_claim_ids"]) != len(set(c["member_raw_claim_ids"]))
        for c in clusters
    ):
        raise ValueError("Incomplete cluster assignment; not silently repaired")
    return clusters


def _cluster_candidate_payload(row: dict[str, object]) -> dict[str, str]:
    return {
        "raw_claim_id": str(row.get("raw_claim_id", "")).strip(),
        "raw_claim_text": _raw_claim_text(row),
        "source_quote_exact": str(row.get("source_quote_exact", "")).strip(),
        "doc_id": str(row.get("doc_id", "")).strip(),
        "chunk_id": str(row.get("chunk_id", "")).strip(),
        "page_number": str(row.get("page_number", "")).strip(),
        "paragraph_id": str(row.get("paragraph_id", "")).strip(),
        "citation_numbers": str(row.get("citation_numbers", "")).strip(),
        "reference_used": str(row.get("reference_used", "")).strip(),
        "evidence_scheme_json": str(row.get("evidence_scheme_json", "")).strip(),
        "claim_type": str(row.get("claim_type", "")).strip(),
        "context_before": str(row.get("prev_sentence_text", "")).strip(),
        "context_after": str(row.get("next_sentence_text", "")).strip(),
        "context_snippet": str(
            row.get("review_context_3sent")
            or row.get("context_snippet")
            or row.get("source_quote_exact")
            or ""
        ).strip(),
    }


def _consensus_clusters_to_structured_claims(
    clusters: list[dict[str, object]],
    *,
    candidates_by_id: dict[str, dict[str, object]],
    doc_type_by_doc_id: dict[str, str],
    config: PipelineConfig,
) -> list[dict[str, object]]:
    # Membership comes exclusively from the model's batch and cross-batch decisions.
    rows: list[dict[str, object]] = []
    for cluster in clusters:
        member_ids = _coerce_member_raw_claim_ids(
            cluster.get("member_raw_claim_ids", [])
        )
        if not str(cluster.get("consensus_claim", "")).strip() or not member_ids:
            raise ValueError("Consensus cluster requires text and member IDs")
        if set(member_ids) - set(candidates_by_id):
            raise ValueError("Consensus cluster contains unknown member IDs")
        consensus_claim = str(cluster["consensus_claim"]).strip()
        member_ids = _coerce_member_raw_claim_ids(cluster["member_raw_claim_ids"])
        member_candidates = [
            candidates_by_id[raw_claim_id] for raw_claim_id in member_ids
        ]
        origin_evidence = _unique_origin_evidence(
            [
                {
                    "raw_claim_id": str(candidate.get("raw_claim_id", "")).strip(),
                    "source_type": str(candidate.get("source_type", "")).strip(),
                    "raw_claim_text": _raw_claim_text(candidate),
                    "source_quote_exact": str(
                        candidate.get("source_quote_exact", "")
                    ).strip(),
                    "doc_id": str(candidate.get("doc_id", "")).strip(),
                    "chunk_id": str(candidate.get("chunk_id", "")).strip(),
                    "page_number": str(candidate.get("page_number", "")).strip(),
                    "paragraph_id": str(candidate.get("paragraph_id", "")).strip(),
                    "citation_numbers": str(
                        candidate.get("citation_numbers", "")
                    ).strip(),
                    "reference_used": str(candidate.get("reference_used", "")).strip(),
                    "evidence_scheme_json": str(
                        candidate.get("evidence_scheme_json", "")
                    ).strip(),
                    "context_snippet": _candidate_context_snippet(candidate),
                }
                for candidate in member_candidates
                if str(candidate.get("doc_id", "")).strip()
            ]
        )
        doc_counts = Counter(
            str(candidate.get("doc_id", "")).strip()
            for candidate in member_candidates
            if str(candidate.get("doc_id", "")).strip()
        )
        doc_ids = set(doc_counts)
        wdf = 0.0
        for doc_id in sorted(doc_ids):
            doc_type = doc_type_by_doc_id.get(doc_id, "research")
            wdf += (
                config.doc_weight_review
                if doc_type == "review"
                else config.doc_weight_original_article
            )
        source_types = sorted(
            {
                str(candidate.get("source_type", "")).strip()
                for candidate in member_candidates
                if str(candidate.get("source_type", "")).strip()
            }
        )
        source_names = sorted(
            {
                str(candidate.get("source_name", "")).strip()
                for candidate in member_candidates
                if str(candidate.get("source_name", "")).strip()
            }
        )
        manual_seed_count = sum(
            1
            for candidate in member_candidates
            if str(candidate.get("source_type", "")).strip() == "manual_seed"
        )
        auto_candidate_count = sum(
            1
            for candidate in member_candidates
            if str(candidate.get("source_type", "")).strip() != "manual_seed"
        )
        claim_type = (
            MANUAL_SEED_CLAIM
            if manual_seed_count > 0 and auto_candidate_count == 0
            else CONSENSUS_CLAIM
        )
        claim_type_reason = (
            "Manual seed claim preserved through LLM clustering."
            if claim_type == MANUAL_SEED_CLAIM
            else str(cluster.get("cluster_reason", "")).strip()
            or "LLM consensus cluster."
        )
        top_citing_docs = "; ".join(
            doc_id
            for doc_id, _count in sorted(
                doc_counts.items(), key=lambda item: (-item[1], item[0])
            )[: config.top_citing_docs_limit]
        )
        rows.append(
            {
                "normalized_claim_id": new_claim_id(),
                "normalized_claim_text": consensus_claim,
                "claim_zh": "",
                "source_types": "; ".join(source_types),
                "source_names": "; ".join(source_names),
                "origin_chunk_ids": "; ".join(
                    item["chunk_id"] for item in origin_evidence if item["chunk_id"]
                ),
                "origin_doc_ids": "; ".join(
                    _unique_preserve_order(
                        [item["doc_id"] for item in origin_evidence if item["doc_id"]]
                    )
                ),
                "origin_evidence_json": json.dumps(origin_evidence, ensure_ascii=False),
                "member_raw_claim_ids": "; ".join(_unique_preserve_order(member_ids)),
                "raw_claim_count": len(member_candidates),
                "manual_seed_count": manual_seed_count,
                "auto_candidate_count": auto_candidate_count,
                "claim_type": claim_type,
                "claim_type_reason": claim_type_reason,
                "normalization_reason": claim_type_reason,
                "consensus_note": (
                    CONSENSUS_CLAIM_NOTE if claim_type == CONSENSUS_CLAIM else ""
                ),
                "mechanism_family": str(cluster.get("mechanism_family", "")).strip(),
                "mechanism_role": str(cluster.get("mechanism_role", "")).strip(),
                "specific_system": str(cluster.get("specific_system", "")).strip(),
                "overlap_type": str(cluster.get("overlap_type", "")).strip(),
                "parent_claim_hint": str(cluster.get("parent_claim_hint", "")).strip(),
                "claim_type_counts": "",
                "mention_density": max(doc_counts.values(), default=0),
                "mention_density_mean": (
                    round(sum(doc_counts.values()) / len(doc_counts), 3)
                    if doc_counts
                    else 0
                ),
                "df": len(doc_ids),
                "wdf": round(wdf, 3),
                "top_citing_docs": top_citing_docs,
                "supporting_doc_ids": "; ".join(sorted(doc_ids)),
            }
        )
    return rows


def _candidate_context_snippet(candidate: dict[str, object]) -> str:
    context = str(candidate.get("review_context_3sent", "")).strip()
    if context:
        return context
    parts = [
        str(candidate.get("prev_sentence_text", "")).strip(),
        _raw_claim_text(candidate),
        str(candidate.get("next_sentence_text", "")).strip(),
    ]
    return " ".join(part for part in parts if part).strip()


def _raw_claim_text(candidate: dict[str, object]) -> str:
    return str(candidate.get("raw_claim_text", "")).strip()


def _write_step3b_cluster_quality_audit(
    context: Step3RunContext,
    structured_claims: list[dict[str, object]],
) -> None:
    rows: list[dict[str, object]] = []
    for claim in structured_claims:
        origin_items = _parse_origin_evidence_items(
            str(claim.get("origin_evidence_json", ""))
        )
        member_raw_claim_ids = [
            str(item.get("raw_claim_id", "")).strip() for item in origin_items
        ]
        sample_contexts = _unique_preserve_order(
            [
                str(item.get("context_snippet", "")).strip()
                for item in origin_items
                if str(item.get("context_snippet", "")).strip()
            ]
        )
        rows.append(
            {
                "normalized_claim_id": str(
                    claim.get("normalized_claim_id", "")
                ).strip(),
                "normalized_claim_text": str(
                    claim.get("normalized_claim_text", "")
                ).strip(),
                "mechanism_family": str(claim.get("mechanism_family", "")).strip(),
                "mechanism_role": str(claim.get("mechanism_role", "")).strip(),
                "specific_system": str(claim.get("specific_system", "")).strip(),
                "overlap_type": str(claim.get("overlap_type", "")).strip(),
                "parent_claim_hint": str(claim.get("parent_claim_hint", "")).strip(),
                "raw_claim_count": str(claim.get("raw_claim_count", "")).strip(),
                "origin_doc_ids": str(claim.get("origin_doc_ids", "")).strip(),
                "origin_chunk_ids": str(claim.get("origin_chunk_ids", "")).strip(),
                "member_raw_claim_ids": "; ".join(
                    _unique_preserve_order(member_raw_claim_ids)
                ),
                "sample_contexts": " || ".join(sample_contexts[:3]),
                "normalization_reason": str(
                    claim.get("normalization_reason", "")
                ).strip(),
            }
        )
    _write_rows(
        context.config.claim_generation_dir / "03B_cluster_quality_audit.csv",
        rows,
        STEP3B_CLUSTER_QUALITY_AUDIT_FIELDS,
    )


def _coerce_member_raw_claim_ids(raw_value: object) -> list[str]:
    if isinstance(raw_value, str):
        return [part.strip() for part in re.split(r"[,;|]", raw_value) if part.strip()]
    if isinstance(raw_value, list):
        return [str(part).strip() for part in raw_value if str(part).strip()]
    return []


def _unique_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _unique_origin_evidence(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str, str]] = set()
    result: list[dict[str, str]] = []
    for item in items:
        key = (
            str(item.get("raw_claim_id", "")).strip(),
            str(item.get("doc_id", "")).strip(),
            str(item.get("chunk_id", "")).strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _run_step3c(
    context: Step3RunContext,
    structured_claims: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    print(
        "[Stage 6] Building source-anchored occurrence rows and preparing generalization review rows..."
    )
    support_rows = _build_origin_occurrence_rows(structured_claims, context.chunk_rows)
    print(f"[OK] Built {len(support_rows)} source-anchored occurrence rows")
    generalized_claims = _attach_source_index_chunks(
        structured_claims, support_rows, context.chunk_rows
    )
    missing_primary_source_count = sum(
        1
        for row in generalized_claims
        if str(row.get("source_chunk_status", "")).strip() == "missing_primary_source"
    )
    if missing_primary_source_count:
        print(
            "[WARN] "
            f"{missing_primary_source_count} claims are missing a primary source chunk; "
            "support occurrence rows remain available for frequency accounting."
        )
    generalized_claims = _backfill_frequency_metrics_from_support_rows(
        generalized_claims,
        support_rows,
        context.doc_type_by_doc_id,
        context.config,
    )
    generalized_claims = _attach_saturation_metrics(
        generalized_claims, context.total_doc_count
    )
    generalized_claims = _ensure_generalized_claim_fields(generalized_claims)
    generalized_claims = _strip_deprecated_generalization_fields(generalized_claims)
    _write_rows(context.config.support_evidence_rows_path, support_rows)
    _write_rows(context.config.step3c_generalization_review_path, generalized_claims)
    print(f"[OK] Wrote {context.config.support_evidence_rows_path.name}")
    print(f"[OK] Wrote {context.config.step3c_generalization_review_path.name}")
    return generalized_claims, support_rows


def _run_step3d(
    context: Step3RunContext,
    generalized_claims: list[dict[str, object]],
    support_rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    print("[Stage 7] Building final consensus outputs...")
    claim_cards = _build_strict_consensus_outputs(
        generalized_claims=generalized_claims,
        support_rows=support_rows,
        doc_type_by_doc_id=context.doc_type_by_doc_id,
        total_doc_count=context.total_doc_count,
        config=context.config,
    )
    all_claim_cards = _rerank_consensus_rows(list(claim_cards))
    all_frequency_rows = [
        {field: row.get(field, "") for field in CONSENSUS_CLAIM_SUMMARY_FIELDS}
        for row in all_claim_cards
    ]
    _write_rows(
        context.config.claim_generation_dir / "03_claims_master_all.csv",
        all_claim_cards,
        CONSENSUS_CLAIM_MASTER_FIELDS,
    )
    _write_rows(
        context.config.claim_generation_dir / "04_consensus_claims_all.csv",
        all_frequency_rows,
        CONSENSUS_CLAIM_SUMMARY_FIELDS,
    )
    claim_cards = _apply_final_consensus_hygiene(context, claim_cards)
    claim_cards = _rerank_consensus_rows(claim_cards)
    frequency_rows = [
        {field: row.get(field, "") for field in CONSENSUS_CLAIM_SUMMARY_FIELDS}
        for row in claim_cards
    ]
    _write_rows(
        context.config.claims_master_path, claim_cards, CONSENSUS_CLAIM_MASTER_FIELDS
    )
    _write_rows(
        context.config.consensus_claims_path,
        frequency_rows,
        CONSENSUS_CLAIM_SUMMARY_FIELDS,
    )
    print(f"[OK] Wrote {context.config.claims_master_path.name}")
    print(f"[OK] Wrote {context.config.consensus_claims_path.name}")
    return claim_cards


def _write_rows(
    path: Path, rows: list[dict[str, object]], fieldnames: list[str] | None = None
) -> None:
    if fieldnames is not None:
        resolved_fieldnames = list(fieldnames)
    else:
        resolved_fieldnames: list[str] = []
        for row in rows:
            for key in row.keys():
                if key not in resolved_fieldnames:
                    resolved_fieldnames.append(key)
    if path.name in {
        "01_raw_claim_candidates.csv",
        "03A_claim_scope_review.csv",
        "03A_quarantined_candidates.csv",
    }:
        resolved_fieldnames = [
            field for field in RAW_CANDIDATE_FIELD_ORDER if field in resolved_fieldnames
        ]
    if path.name == "01_raw_claim_candidates.csv":
        ordered = [
            field for field in RAW_CANDIDATE_FIELD_ORDER if field in resolved_fieldnames
        ]
        ordered.extend(field for field in resolved_fieldnames if field not in ordered)
        resolved_fieldnames = ordered
    sanitized_rows = [
        {field: row.get(field, "") for field in resolved_fieldnames} for row in rows
    ]
    write_csv(path, sanitized_rows, resolved_fieldnames)


def _build_strict_consensus_outputs(
    *,
    generalized_claims: list[dict[str, object]],
    support_rows: list[dict[str, object]],
    doc_type_by_doc_id: dict[str, str],
    total_doc_count: int,
    config: PipelineConfig,
) -> list[dict[str, object]]:
    support_doc_counts_by_generalized_id: dict[str, Counter[str]] = {}
    support_doc_counts_by_normalized_id: dict[str, Counter[str]] = {}
    for row in support_rows:
        doc_id = str(row.get("doc_id", "")).strip()
        if not doc_id:
            continue
        generalized_claim_id = str(row.get("generalized_claim_id", "")).strip()
        normalized_claim_id = str(row.get("normalized_claim_id", "")).strip()
        if generalized_claim_id:
            support_doc_counts_by_generalized_id.setdefault(
                generalized_claim_id, Counter()
            )[doc_id] += 1
        if normalized_claim_id:
            support_doc_counts_by_normalized_id.setdefault(
                normalized_claim_id, Counter()
            )[doc_id] += 1

    groups: dict[str, dict[str, object]] = {}
    for claim in generalized_claims:
        generalized_claim_id = str(claim.get("generalized_claim_id", "")).strip()
        generalized_claim_text = str(claim.get("generalized_claim_text", "")).strip()
        normalized_claim_id = str(claim.get("normalized_claim_id", "")).strip()
        normalized_claim_text = str(claim.get("normalized_claim_text", "")).strip()
        consensus_claim_text = generalized_claim_text or normalized_claim_text
        consensus_claim_id = generalized_claim_id or (
            new_claim_id() if consensus_claim_text else normalized_claim_id
        )
        if not consensus_claim_id:
            continue

        group = groups.setdefault(
            consensus_claim_id,
            {
                "consensus_claim_id": consensus_claim_id,
                "consensus_claim_text": consensus_claim_text,
                "source_generalized_claim_ids": [],
                "source_normalized_claim_ids": [],
                "source_raw_claim_ids": [],
                "source_chunk_ids": [],
                "origin_doc_ids": [],
                "origin_evidence": [],
                "reference_items": [],
                "claim_statuses": [],
                "claim_status_reasons": [],
                "doc_counter": Counter(),
                "fallback_doc_ids": [],
                "wdf_hint": 0.0,
            },
        )
        if (
            consensus_claim_text
            and not str(group.get("consensus_claim_text", "")).strip()
        ):
            group["consensus_claim_text"] = consensus_claim_text

        _append_unique(
            group["source_generalized_claim_ids"],
            generalized_claim_id or consensus_claim_id,
        )
        _append_unique(group["source_normalized_claim_ids"], normalized_claim_id)
        for raw_claim_id in _split_semicolon_values(
            str(claim.get("member_raw_claim_ids", ""))
        ):
            _append_unique(group["source_raw_claim_ids"], raw_claim_id)
        for chunk_id in _split_semicolon_values(str(claim.get("origin_chunk_ids", ""))):
            _append_unique(group["source_chunk_ids"], chunk_id)
        for doc_id in _split_semicolon_values(str(claim.get("origin_doc_ids", ""))):
            _append_unique(group["origin_doc_ids"], doc_id)
            _append_unique(group["fallback_doc_ids"], doc_id)

        origin_items = _parse_origin_evidence_items(
            str(claim.get("origin_evidence_json", "")).strip()
        )
        for item in origin_items:
            _append_unique_evidence_item(group["origin_evidence"], item)
            raw_claim_id = str(item.get("raw_claim_id", "")).strip()
            chunk_id = str(item.get("chunk_id", "")).strip()
            doc_id = str(item.get("doc_id", "")).strip()
            if raw_claim_id:
                _append_unique(group["source_raw_claim_ids"], raw_claim_id)
            if chunk_id:
                _append_unique(group["source_chunk_ids"], chunk_id)
            if doc_id:
                _append_unique(group["origin_doc_ids"], doc_id)
                _append_unique(group["fallback_doc_ids"], doc_id)
            for reference_item in _extract_reference_items_from_origin(item):
                _append_unique_reference_item(group["reference_items"], reference_item)

        for reference_item in _parse_reference_items(
            str(claim.get("reference_used", "")).strip()
        ):
            _append_unique_reference_item(group["reference_items"], reference_item)

        status = str(claim.get("generalization_status", "")).strip() or "supported"
        reason = str(claim.get("generalization_reason", "")).strip()
        _append_unique(group["claim_statuses"], status)
        _append_unique(group["claim_status_reasons"], reason)

        doc_counter = Counter()
        if generalized_claim_id:
            doc_counter.update(
                support_doc_counts_by_generalized_id.get(
                    generalized_claim_id, Counter()
                )
            )
        if normalized_claim_id:
            doc_counter.update(
                support_doc_counts_by_normalized_id.get(normalized_claim_id, Counter())
            )
        group["doc_counter"].update(doc_counter)
        group["wdf_hint"] = max(
            float(group["wdf_hint"]), _safe_float(claim.get("wdf", 0))
        )

    rows = [
        _finalize_strict_consensus_group(
            group, doc_type_by_doc_id, total_doc_count, config
        )
        for group in groups.values()
    ]
    return _rerank_consensus_rows(rows)


def _rerank_consensus_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    reranked = [dict(row) for row in rows]
    reranked.sort(key=_strict_consensus_sort_key)
    for index, row in enumerate(reranked, start=1):
        row["consensus_rank"] = index
    return reranked


def _apply_final_consensus_hygiene(
    context: Step3RunContext,
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    review_rows: list[dict[str, object]] = []
    kept_rows: list[dict[str, object]] = []
    for row in rows:
        keep, reason, entity_focus = _final_consensus_hygiene_decision(
            row,
            claim_min_df=int(getattr(context.config, "claim_min_df", 2) or 2),
        )
        review_rows.append(
            {
                "consensus_claim_id": row.get("consensus_claim_id", ""),
                "consensus_claim_text": row.get("consensus_claim_text", ""),
                "df": row.get("df", ""),
                "raw_claim_count": row.get("raw_claim_count", ""),
                "entity_focus": entity_focus,
                "decision": "keep" if keep else "drop",
                "decision_reason": reason,
            }
        )
        if keep:
            kept_rows.append(row)
    hygiene_path = (
        context.config.claim_generation_dir / "04A_consensus_hygiene_review.csv"
    )
    _write_rows(hygiene_path, review_rows, CONSENSUS_HYGIENE_REVIEW_FIELDS)
    print(f"[OK] Consensus hygiene retained {len(kept_rows)}/{len(rows)} rows")
    print(f"[OK] Wrote {hygiene_path.name}")
    return kept_rows


def _final_consensus_hygiene_decision(
    row: dict[str, object], *, claim_min_df: int
) -> tuple[bool, str, str]:
    """Apply only structural and declared frequency requirements after model review."""
    text = str(row.get("consensus_claim_text", "")).strip()
    if not text:
        return False, "empty_consensus_claim_text", "model_reviewed"
    df = _safe_int(row.get("df", 0))
    if df < max(1, int(claim_min_df)):
        return (
            False,
            f"document_frequency_below_threshold(df={df}, min_df={claim_min_df})",
            "model_reviewed",
        )
    return True, "model_accepted_and_frequency_eligible", "model_reviewed"


def _finalize_strict_consensus_group(
    group: dict[str, object],
    doc_type_by_doc_id: dict[str, str],
    total_doc_count: int,
    config: PipelineConfig,
) -> dict[str, object]:
    doc_counter = Counter(group.get("doc_counter", Counter()))
    if doc_counter:
        ranked_doc_ids = [
            doc_id
            for doc_id, _count in sorted(
                doc_counter.items(), key=lambda item: (-item[1], item[0])
            )
        ]
    else:
        ranked_doc_ids = list(group.get("fallback_doc_ids", []))

    wdf = 0.0
    for doc_id in ranked_doc_ids:
        doc_type = doc_type_by_doc_id.get(doc_id, "research")
        wdf += (
            config.doc_weight_review
            if doc_type == "review"
            else config.doc_weight_original_article
        )
    if not ranked_doc_ids:
        wdf = float(group.get("wdf_hint", 0.0) or 0.0)

    source_raw_claim_ids = list(group.get("source_raw_claim_ids", []))
    df = len(ranked_doc_ids)
    saturation_score = round((df / total_doc_count) if total_doc_count > 0 else 0.0, 6)
    top_citing_docs = "; ".join(ranked_doc_ids[: config.top_citing_docs_limit])
    statuses = [
        value for value in group.get("claim_statuses", []) if str(value).strip()
    ]
    claim_status = (
        "supported" if not statuses or "supported" in statuses else str(statuses[0])
    )
    claim_status_reason = "; ".join(
        str(value)
        for value in group.get("claim_status_reasons", [])
        if str(value).strip()
    )
    reference_used = json.dumps(group.get("reference_items", []), ensure_ascii=False)
    origin_evidence_json = json.dumps(
        group.get("origin_evidence", []), ensure_ascii=False
    )
    raw_claim_count = len(source_raw_claim_ids)
    if raw_claim_count == 0:
        raw_claim_count = sum(
            1
            for item in group.get("origin_evidence", [])
            if isinstance(item, dict) and str(item.get("raw_claim_id", "")).strip()
        )

    return {
        "consensus_claim_id": group.get("consensus_claim_id", ""),
        "consensus_claim_text": group.get("consensus_claim_text", ""),
        "source_generalized_claim_ids": "; ".join(
            group.get("source_generalized_claim_ids", [])
        ),
        "source_normalized_claim_ids": "; ".join(
            group.get("source_normalized_claim_ids", [])
        ),
        "source_raw_claim_ids": "; ".join(source_raw_claim_ids),
        "source_chunk_ids": "; ".join(group.get("source_chunk_ids", [])),
        "origin_doc_ids": "; ".join(group.get("origin_doc_ids", [])),
        "origin_evidence_json": origin_evidence_json,
        "reference_used": reference_used,
        "claim_status": claim_status,
        "claim_status_reason": claim_status_reason,
        "generalized_claim_count": len(group.get("source_generalized_claim_ids", [])),
        "normalized_claim_count": len(group.get("source_normalized_claim_ids", [])),
        "raw_claim_count": raw_claim_count,
        "df": df,
        "wdf": round(wdf, 3),
        "total_doc_count": total_doc_count,
        "saturation_score": saturation_score,
        "top_citing_docs": top_citing_docs,
        "supporting_doc_ids": "; ".join(ranked_doc_ids),
        "consensus_rank": "",
        "frequency_evidence": _format_frequency_evidence(
            df=float(df),
            wdf=float(wdf),
            total_doc_count=int(total_doc_count or 0),
            saturation_score=float(saturation_score),
            top_citing_docs=top_citing_docs,
            raw_claim_count=int(raw_claim_count or 0),
        ),
    }


def _strict_consensus_sort_key(row: dict[str, object]) -> tuple[int, str]:
    return (-_safe_int(row.get("df", 0)), str(row.get("consensus_claim_text", "")))


def _split_semicolon_values(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split(";") if part.strip()]


def _append_unique(values: list[object], value: object) -> None:
    text = str(value or "").strip()
    if text and text not in values:
        values.append(text)


def _append_unique_evidence_item(values: list[object], item: dict[str, object]) -> None:
    raw_claim_id = str(item.get("raw_claim_id", "")).strip()
    chunk_id = str(item.get("chunk_id", "")).strip()
    doc_id = str(item.get("doc_id", "")).strip()
    key = (raw_claim_id, chunk_id, doc_id)
    for existing in values:
        if not isinstance(existing, dict):
            continue
        existing_key = (
            str(existing.get("raw_claim_id", "")).strip(),
            str(existing.get("chunk_id", "")).strip(),
            str(existing.get("doc_id", "")).strip(),
        )
        if existing_key == key:
            return
    values.append(item)


def _append_unique_reference_item(
    values: list[object], item: dict[str, object]
) -> None:
    reference_number = str(item.get("reference_number", "")).strip()
    reference_text = str(item.get("reference_text", "")).strip()
    key = (reference_number, reference_text)
    if not reference_number and not reference_text:
        return
    for existing in values:
        if not isinstance(existing, dict):
            continue
        existing_key = (
            str(existing.get("reference_number", "")).strip(),
            str(existing.get("reference_text", "")).strip(),
        )
        if existing_key == key:
            return
    values.append(
        {"reference_number": reference_number, "reference_text": reference_text}
    )


def _extract_reference_items_from_origin(
    item: dict[str, object],
) -> list[dict[str, object]]:
    reference_items = _parse_reference_items(
        str(item.get("reference_used", "")).strip()
    )
    if reference_items:
        return reference_items
    evidence_scheme = str(item.get("evidence_scheme_json", "")).strip()
    if not evidence_scheme:
        return []
    try:
        parsed_scheme = json.loads(evidence_scheme)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed_scheme, dict):
        return []
    references_used = parsed_scheme.get("references_used", [])
    if not isinstance(references_used, list):
        return []
    return [
        {
            "reference_number": str(ref.get("reference_number", "")).strip(),
            "reference_text": str(ref.get("reference_text", "")).strip(),
        }
        for ref in references_used
        if isinstance(ref, dict)
    ]


def _parse_reference_items(value: str) -> list[dict[str, object]]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [
        {
            "reference_number": str(item.get("reference_number", "")).strip(),
            "reference_text": str(item.get("reference_text", "")).strip(),
        }
        for item in parsed
        if isinstance(item, dict)
    ]


def _write_claim_scope_review_artifacts(
    config: PipelineConfig, raw_candidates: list[dict[str, object]]
) -> None:
    review_fields = [
        "raw_claim_id",
        "source_type",
        "source_name",
        "doc_id",
        "chunk_id",
        "page_number",
        "paragraph_id",
        "raw_claim_text",
        "extracted_claim_text",
        "source_quote_exact",
        "citation_evidence_span",
        "citation_numbers",
        "reference_used",
        "evidence_scheme_json",
        "keep_or_drop",
        "drop_reason",
        "claim_kind",
        "knowledge_scope",
        "knowledge_scope_reason",
        "source_anchor_valid",
        "is_entity_related",
        "entity_relation_summary",
        "entity_role_freeform",
        "keep_for_entity_project",
        "keep_reason",
        "teacher_final_claim_ready",
        "teacher_reject_scope",
        "teacher_review_reason",
        "guardrail_final_claim_ready",
        "guardrail_reason",
        "project_focus_ready",
        "project_focus_reason",
        "filter_reasoning",
        "claim_type",
        "claim_type_reason",
        "stage3a_accepted",
        "anchor_sentence_text",
        "prev_sentence_text",
        "next_sentence_text",
        "review_context_3sent",
    ]
    review_rows = [
        {field: row.get(field, "") for field in review_fields} for row in raw_candidates
    ]
    quarantined_rows = [
        row
        for row in review_rows
        if str(row.get("source_type", "")).strip() != "manual_seed"
        and str(row.get("stage3a_accepted", "")).strip().lower() != "true"
    ]
    _write_rows(config.claim_scope_review_path, review_rows, review_fields)
    _write_rows(
        config.quarantined_claim_candidates_path, quarantined_rows, review_fields
    )


def _build_doc_type_map(
    chunk_rows: list[dict[str, str]], doc_rows: list[dict[str, str]]
) -> dict[str, str]:
    doc_type_by_doc_id = {
        row["doc_id"]: row.get("doc_type", "") for row in doc_rows if row.get("doc_id")
    }
    for row in chunk_rows:
        doc_type_by_doc_id.setdefault(row["doc_id"], row.get("doc_type", ""))
    return doc_type_by_doc_id


def _ensure_generalized_claim_fields(
    claims: list[dict[str, object]],
) -> list[dict[str, object]]:
    enriched_claims: list[dict[str, object]] = []
    for claim in claims:
        updated = dict(claim)
        normalized_claim_id = str(updated.get("normalized_claim_id", "")).strip()
        normalized_claim_text = str(updated.get("normalized_claim_text", "")).strip()
        if (
            normalized_claim_id
            and not str(updated.get("generalized_claim_id", "")).strip()
        ):
            updated["generalized_claim_id"] = normalized_claim_id
        if (
            normalized_claim_text
            and not str(updated.get("generalized_claim_text", "")).strip()
        ):
            updated["generalized_claim_text"] = normalized_claim_text
        if not str(updated.get("generalization_status", "")).strip():
            updated["generalization_status"] = "pending_generalization_review"
        if not str(updated.get("generalization_reason", "")).strip():
            updated["generalization_reason"] = (
                "Initialized from Step 3B normalized claim."
            )
        enriched_claims.append(updated)
    return enriched_claims


def _strip_deprecated_generalization_fields(
    claims: list[dict[str, object]],
) -> list[dict[str, object]]:
    retired_generalization_prefix = "broad_" + "claim"
    deprecated_fields = {
        retired_generalization_prefix + "_id",
        retired_generalization_prefix,
        retired_generalization_prefix + "_status",
        retired_generalization_prefix + "_reason",
        "claim_id",
        "canonical_claim",
        "raw_candidate_count",
        "member_candidate_ids",
        "origin_candidate_id",
        "source_quote",
    }
    return [
        {
            field: value
            for field, value in claim.items()
            if field not in deprecated_fields
        }
        for claim in claims
    ]


def _safe_int(value: object, *, default: int = 0) -> int:
    try:
        return int(float(str(value or "").strip()))
    except ValueError:
        return default


def _safe_float(value: object, *, default: float = 0.0) -> float:
    try:
        return float(str(value or "").strip())
    except ValueError:
        return default


def _backfill_frequency_metrics_from_support_rows(
    normalized_claims: list[dict[str, object]],
    support_rows: list[dict[str, object]],
    doc_type_by_doc_id: dict[str, str],
    config: PipelineConfig,
) -> list[dict[str, object]]:
    support_rows_by_normalized_claim_id: dict[str, list[dict[str, object]]] = {}
    for row in support_rows:
        normalized_claim_id = str(row.get("normalized_claim_id", "")).strip()
        if not normalized_claim_id:
            continue
        support_rows_by_normalized_claim_id.setdefault(normalized_claim_id, []).append(
            row
        )

    enriched_rows: list[dict[str, object]] = []
    for claim in normalized_claims:
        updated = dict(claim)
        claim_support_rows = support_rows_by_normalized_claim_id.get(
            str(claim.get("normalized_claim_id", "")), []
        )
        support_doc_counts: dict[str, int] = {}
        for row in claim_support_rows:
            doc_id = str(row.get("doc_id", "")).strip()
            if not doc_id:
                continue
            support_doc_counts[doc_id] = support_doc_counts.get(doc_id, 0) + 1

        if support_doc_counts:
            ranked_doc_ids = [
                doc_id
                for doc_id, _count in sorted(
                    support_doc_counts.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ]
            wdf = 0.0
            for doc_id in ranked_doc_ids:
                doc_type = doc_type_by_doc_id.get(doc_id, "research")
                wdf += (
                    config.doc_weight_review
                    if doc_type == "review"
                    else config.doc_weight_original_article
                )

            updated["df"] = len(ranked_doc_ids)
            updated["wdf"] = round(wdf, 3)
            updated["top_citing_docs"] = "; ".join(
                ranked_doc_ids[: config.top_citing_docs_limit]
            )
            updated["supporting_doc_ids"] = "; ".join(ranked_doc_ids)
        else:
            updated["df"] = 0
            updated["wdf"] = 0.0
            updated["top_citing_docs"] = ""
            updated["supporting_doc_ids"] = ""
        enriched_rows.append(updated)

    return enriched_rows


def _resolve_total_doc_count(
    doc_rows: list[dict[str, str]],
    chunk_rows: list[dict[str, str]],
) -> int:
    doc_ids = {
        str(row.get("doc_id", "")).strip()
        for row in doc_rows
        if str(row.get("doc_id", "")).strip()
    }
    if not doc_ids:
        doc_ids = {
            str(row.get("doc_id", "")).strip()
            for row in chunk_rows
            if str(row.get("doc_id", "")).strip()
        }
    return len(doc_ids)


def _attach_saturation_metrics(
    normalized_claims: list[dict[str, object]],
    total_doc_count: int,
) -> list[dict[str, object]]:
    enriched_rows: list[dict[str, object]] = []
    for claim in normalized_claims:
        updated = dict(claim)
        df = float(updated.get("df", 0) or 0)
        saturation_score = (df / total_doc_count) if total_doc_count > 0 else 0.0
        updated["total_doc_count"] = total_doc_count
        updated["saturation_score"] = round(saturation_score, 6)
        enriched_rows.append(updated)
    return enriched_rows


def _format_frequency_evidence(
    *,
    df: float,
    wdf: float,
    total_doc_count: int,
    saturation_score: float,
    top_citing_docs: str,
    raw_claim_count: int,
) -> str:
    return (
        f"df={int(df)}; wdf={wdf:.1f}; total_docs={int(total_doc_count)}; "
        f"s={saturation_score:.4f}; top_docs={top_citing_docs}; raw_claims={raw_claim_count}"
    )
