from __future__ import annotations
import asyncio
from shared import checkpoint_journal
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from shared.settings import PipelineConfig
from shared.storage import atomic_write_text, read_csv, write_csv
from claims.model_tasks import (
    run_llm_chunk_batch,
    run_llm_chunk_batch_async,
    run_llm_entity_integrated_claim_processor,
    run_llm_entity_integrated_claim_processor_async,
)
from scoring.references.reference_mapping import build_raw_reference_scheme

CONSENSUS_CLAIM = "consensus_claim"
MANUAL_SEED_CLAIM = "manual_seed"
LOCAL_RESULT = "local_result"
_REQUIRED_LLM_ENV_KEYS = ("LLM_API_KEY", "LLM_API_BASE_URL", "LLM_MODEL_NAME")


def discover_raw_claim_candidates(
    config: PipelineConfig,
    seed_claims: list[dict[str, str]],
    chunk_rows: list[dict[str, str]],
    doc_rows: list[dict[str, str]],
    llm_env: dict[str, str] | None,
    review_llm_env: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    llm_env = _require_configured_llm_env(llm_env)
    _require_configured_llm_env(review_llm_env or llm_env)
    candidates: list[dict[str, object]] = []
    raw_claim_index = 0
    from shared.selected_entity import require_entity
    from claims.source_selection import select_target_chunks

    if not config.skip_document_claim_extraction:
        raise ValueError(
            "Name-hit chunk extraction cannot enable an additional whole-document pass"
        )
    chunk_rows, doc_rows, source_scope = select_target_chunks(
        chunk_rows, doc_rows, require_entity()
    )
    scope_path = config.claim_generation_dir / "source_chunk_selection.json"
    atomic_write_text(
        scope_path, json.dumps(source_scope, ensure_ascii=False, indent=2)
    )
    print(
        f"[Stage 4] Upstream-name retrieval retained {source_scope['selected_nonbibliographic_chunks']}/{source_scope['input_nonbibliographic_chunks']} chunks from {source_scope['selected_document_count']} documents"
    )
    doc_context_by_doc_id = _build_doc_context_by_doc_id(
        doc_rows=doc_rows, chunk_rows=chunk_rows
    )
    fulltext_by_doc_id = _build_fulltext_by_doc_id(doc_rows)
    chunk_rows_by_doc_id = _build_chunk_rows_by_doc_id(chunk_rows)
    contextual_chunk_rows = [
        _with_doc_context(row, doc_context_by_doc_id.get(str(row.get("doc_id", ""))))
        for row in chunk_rows
    ]
    contextual_doc_rows = [
        _with_doc_context(row, doc_context_by_doc_id.get(str(row.get("doc_id", ""))))
        for row in doc_rows
    ]
    if seed_claims:
        raise ValueError(
            "Manual claims cannot bypass model acceptance in this workflow"
        )
    selected_chunk_sources = _load_selected_chunk_sources_cache(config)
    if selected_chunk_sources:
        eligible_ids = {str(row["chunk_id"]) for row in chunk_rows}
        if any(
            str(row["chunk_id"]) not in eligible_ids
            for row, text in selected_chunk_sources
        ):
            raise ValueError(
                "Source cache contains chunks outside the upstream-name selection; use a new extraction run"
            )
    if not selected_chunk_sources:
        selected_chunk_sources = _select_claim_discovery_sources(
            contextual_chunk_rows,
            text_field="clean_text",
            fallback_field="text",
            max_sentences=8,
            budget=config.claim_discovery_chunk_budget,
        )
        _write_selected_chunk_sources_cache(config, selected_chunk_sources)
    total_chunks = len(selected_chunk_sources)
    print(f"[OK] Selected {total_chunks} chunks for claim extraction")
    step3a_provider_pool = _load_step3a_provider_pool(llm_env)

    def _append_processed_candidates(
        *,
        results: list[dict[str, object]],
        source_row: dict[str, str],
        processed_items: list[dict[str, str]],
        source_type: str,
        anchor_row: dict[str, str] | None = None,
        review_source_text: str = "",
    ) -> None:
        resolved_anchor_row = anchor_row or source_row
        source_name = str(
            source_row.get("filename", source_row.get("doc_id", ""))
        ).strip()
        for item in processed_items:
            filtered_item = _prepare_entity_integrated_processed_item(
                item, {**source_row, "model_source_text": review_source_text}
            )
            filtered_item["raw_claim_text"] = _final_candidate_claim_text(filtered_item)
            skip_raw, _skip_reason = _should_skip_raw_candidate(filtered_item)
            if skip_raw:
                continue
            filtered_item = _apply_project_focus_gate(filtered_item)
            keep_candidate, keep_reason = _should_keep_extracted_claim(filtered_item)
            claim_type, claim_type_reason = _classify_extracted_claim_with_gate(
                filtered_item
            )
            anchor_text = (
                str(filtered_item.get("citation_evidence_span", "")).strip()
                or str(filtered_item.get("source_quote_exact", "")).strip()
                or str(filtered_item.get("raw_claim_text", "")).strip()
            )
            extracted_claim_text = _final_candidate_claim_text(filtered_item)
            source_quote_exact = (
                str(filtered_item.get("source_quote_exact", "")).strip()
                or str(filtered_item.get("citation_evidence_span", "")).strip()
            )
            row = {
                "source_type": source_type,
                "source_name": source_name,
                "doc_id": resolved_anchor_row.get(
                    "doc_id", source_row.get("doc_id", "")
                ),
                "chunk_id": resolved_anchor_row.get("chunk_id", ""),
                "page_number": resolved_anchor_row.get("page_number", ""),
                "paragraph_id": resolved_anchor_row.get("paragraph_id", ""),
                "raw_claim_text": extracted_claim_text,
                "extracted_claim_text": extracted_claim_text,
                "source_quote_exact": source_quote_exact,
                "claim_zh": "",
                "claim_type": claim_type,
                "citation_evidence_span": filtered_item.get(
                    "citation_evidence_span", ""
                ),
                "citation_numbers": filtered_item.get("citation_numbers", ""),
                "keep_or_drop": filtered_item.get("keep_or_drop", ""),
                "drop_reason": filtered_item.get("drop_reason", ""),
                "claim_kind": filtered_item.get("claim_kind", ""),
                "filter_reasoning": filtered_item.get("filter_reasoning", ""),
                "knowledge_scope": filtered_item.get("knowledge_scope", ""),
                "knowledge_scope_reason": filtered_item.get(
                    "knowledge_scope_reason", ""
                ),
                "source_anchor_valid": filtered_item.get("source_anchor_valid", ""),
                "entity_relation_summary": filtered_item.get(
                    "entity_relation_summary", ""
                ),
                "entity_role_freeform": filtered_item.get("entity_role_freeform", ""),
                "keep_for_entity_project": filtered_item.get(
                    "keep_for_entity_project", ""
                ),
                "keep_reason": filtered_item.get("keep_reason", ""),
                "teacher_final_claim_ready": filtered_item.get(
                    "teacher_final_claim_ready", ""
                ),
                "teacher_reject_scope": filtered_item.get("teacher_reject_scope", ""),
                "teacher_review_reason": filtered_item.get("teacher_review_reason", ""),
                "guardrail_final_claim_ready": filtered_item.get(
                    "guardrail_final_claim_ready", ""
                ),
                "guardrail_reason": filtered_item.get("guardrail_reason", ""),
                "project_focus_ready": filtered_item.get("project_focus_ready", ""),
                "project_focus_reason": filtered_item.get("project_focus_reason", ""),
                "claim_type_reason": f"{claim_type_reason}; extraction_gate={keep_reason}",
                "stage3a_accepted": "true" if keep_candidate else "false",
                "stage3a_reason": keep_reason,
                "source_quote_status": filtered_item.get(
                    "source_quote_status", "unknown"
                ),
                "model_source_anchor_valid": filtered_item.get(
                    "model_source_anchor_valid", "unknown"
                ),
                "source_quote_start": filtered_item.get("source_quote_start", -1),
                "source_quote_end": filtered_item.get("source_quote_end", -1),
            }
            if str(row.get("chunk_id", "")).strip():
                row.update(
                    _raw_chunk_evidence_scheme(
                        resolved_anchor_row,
                        fulltext_by_doc_id,
                        raw_claim_text=str(row.get("raw_claim_text", "")),
                        citation_numbers=filtered_item.get("citation_numbers", ""),
                        citation_evidence_span=source_quote_exact,
                    )
                )
            else:
                row["reference_used"] = "[]"
                row["evidence_scheme_json"] = ""
            row.update(_text_review_context_fields(review_source_text, anchor_text))
            results.append(row)

    def batch_payload(batch):
        return [{"chunk_id": row["chunk_id"], "text": text} for row, text in batch]

    def process_results(batch, response):
        results = []
        for chunk, text in batch:
            rows = []
            _append_processed_candidates(
                results=rows,
                source_row=chunk,
                processed_items=response[chunk["chunk_id"]],
                source_type="auto_chunk",
                anchor_row=chunk,
                review_source_text=text,
            )
            results.append((chunk["chunk_id"], rows))
        return results

    def process_batch(args):
        index, batch = args
        provider = _provider_for_chunk(step3a_provider_pool, index)
        attempts = max(1, config.claim_discovery_provider_max_attempts)
        for attempt in range(attempts):
            try:
                response = run_llm_chunk_batch(
                    provider["LLM_API_KEY"],
                    provider["LLM_API_BASE_URL"],
                    provider["LLM_MODEL_NAME"],
                    sources=batch_payload(batch),
                    source_context=_build_teacher_source_context(batch[0][0]),
                    max_claims=config.claim_discovery_chunk_max_claims,
                )
                return process_results(batch, response)
            except RuntimeError:
                if attempt + 1 == attempts:
                    raise
                time.sleep(
                    config.claim_discovery_provider_retry_backoff_seconds
                    * (attempt + 1)
                )

    async def process_batch_async(args, semaphore):
        index, batch = args
        provider = _provider_for_chunk(step3a_provider_pool, index)
        async with semaphore:
            attempts = max(1, config.claim_discovery_provider_max_attempts)
            for attempt in range(attempts):
                try:
                    response = await run_llm_chunk_batch_async(
                        provider["LLM_API_KEY"],
                        provider["LLM_API_BASE_URL"],
                        provider["LLM_MODEL_NAME"],
                        sources=batch_payload(batch),
                        source_context=_build_teacher_source_context(batch[0][0]),
                        max_claims=config.claim_discovery_chunk_max_claims,
                    )
                    return process_results(batch, response)
                except RuntimeError:
                    if attempt + 1 == attempts:
                        raise
                    await asyncio.sleep(
                        config.claim_discovery_provider_retry_backoff_seconds
                        * (attempt + 1)
                    )

    checkpoint_rows, completed_chunk_ids = _load_raw_claim_checkpoint(config)
    eligible_chunk_ids = {str(row["chunk_id"]) for row, text in selected_chunk_sources}
    if not completed_chunk_ids.issubset(eligible_chunk_ids):
        raise ValueError(
            "Checkpoint sources differ from the current retrieval scope; use a new extraction run"
        )
    for row in checkpoint_rows:
        raw_claim_index += 1
        row["raw_claim_id"] = f"RC{raw_claim_index:04d}"
        candidates.append(row)
    # Pack before removing completed chunks so a resumed run does not join distant sources.
    batches = [
        [
            (row, text)
            for row, text in batch
            if row["chunk_id"] not in completed_chunk_ids
        ]
        for batch in _pack_claim_sources(
            selected_chunk_sources,
            max_chunks=config.claim_discovery_batch_chunks,
            max_chars=config.claim_discovery_batch_chars,
        )
    ]
    batches = [batch for batch in batches if batch]
    batch_args = list(enumerate(batches, 1))
    print(
        f"[Stage 4] {total_chunks} source chunks; {len(batches)} pending model batches; {len(completed_chunk_ids)} completed chunks"
    )
    workers = max(1, min(int(config.claim_discovery_chunk_workers), len(batches)))

    def commit_results(results):
        nonlocal raw_claim_index
        for chunk_id, rows in results:
            _commit_chunk_checkpoint(config, rows, chunk_id)
            completed_chunk_ids.add(chunk_id)
            for row in rows:
                raw_claim_index += 1
                row["raw_claim_id"] = f"RC{raw_claim_index:04d}"
                candidates.append(row)

    if batch_args:
        if _step3_async_enabled():

            async def run_batches():
                semaphore = asyncio.Semaphore(workers)
                tasks = [
                    asyncio.create_task(process_batch_async(arg, semaphore))
                    for arg in batch_args
                ]
                failures = []
                for task in asyncio.as_completed(tasks):
                    try:
                        results = await task
                    except Exception as exc:
                        failures.append(
                            {
                                "error_type": type(exc).__name__,
                                "message": str(exc)[:1000],
                            }
                        )
                        continue
                    commit_results(results)
                if failures:
                    atomic_write_text(
                        config.raw_claim_candidates_path.parent / "chunk_failures.json",
                        json.dumps(failures, indent=2),
                    )
                    raise RuntimeError(
                        f"{len(failures)} extraction batches failed; successful source checkpoints preserved"
                    )

            asyncio.run(run_batches())
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                for future in as_completed(
                    [executor.submit(process_batch, arg) for arg in batch_args]
                ):
                    commit_results(future.result())
    selected_doc_sources = (
        []
        if getattr(config, "skip_document_claim_extraction", False)
        else _select_claim_discovery_sources(
            contextual_doc_rows,
            text_field="doc_clean_text",
            fallback_field="doc_text",
            max_sentences=18,
            budget=config.claim_discovery_doc_budget,
        )
    )
    total_docs = len(selected_doc_sources)
    print(f"[OK] Selected {total_docs} documents for claim extraction")

    def _process_doc(args: tuple[int, dict[str, str], str]) -> list[dict[str, object]]:
        doc_idx, doc_row, extraction_text = args
        if doc_idx % 10 == 0 or doc_idx == 1:
            print(f"  Processing document {doc_idx}/{total_docs}...")
        try:
            chunk_llm_env = _provider_for_chunk(step3a_provider_pool, doc_idx)
            processed_items = _retry_integrated_claim_processor(
                chunk_llm_env,
                config=config,
                text=extraction_text,
                scope="document",
                max_claims=config.claim_discovery_doc_max_claims,
                source_context=_build_teacher_source_context(doc_row),
                max_attempts=config.claim_discovery_provider_max_attempts,
                retry_backoff_seconds=config.claim_discovery_provider_retry_backoff_seconds,
            )
        except RuntimeError as exc:
            doc_id = str(doc_row.get("doc_id", "")).strip() or f"document_{doc_idx}"
            raise RuntimeError(
                f"Claim extraction failed for {doc_id}; source is not completed"
            ) from exc
        results: list[dict[str, object]] = []
        for item in processed_items:
            anchor_text = (
                str(item.get("citation_evidence_span", "")).strip()
                or str(item.get("raw_claim_text", "")).strip()
            )
            anchor_row = _resolve_document_source_anchor(
                doc_id=str(doc_row.get("doc_id", "")).strip(),
                anchor_text=anchor_text,
                chunk_rows_by_doc_id=chunk_rows_by_doc_id,
            )
            _append_processed_candidates(
                results=results,
                source_row=doc_row,
                processed_items=[item],
                source_type="auto_doc",
                anchor_row=anchor_row,
                review_source_text=extraction_text,
            )
        return results

    async def _process_doc_async(
        args: tuple[int, dict[str, str], str], semaphore: asyncio.Semaphore
    ) -> list[dict[str, object]]:
        doc_idx, doc_row, extraction_text = args
        if doc_idx % 10 == 0 or doc_idx == 1:
            print(f"  Processing document {doc_idx}/{total_docs}...")
        async with semaphore:
            try:
                chunk_llm_env = _provider_for_chunk(step3a_provider_pool, doc_idx)
                processed_items = await _retry_integrated_claim_processor_async(
                    chunk_llm_env,
                    config=config,
                    text=extraction_text,
                    scope="document",
                    max_claims=config.claim_discovery_doc_max_claims,
                    source_context=_build_teacher_source_context(doc_row),
                    max_attempts=config.claim_discovery_provider_max_attempts,
                    retry_backoff_seconds=config.claim_discovery_provider_retry_backoff_seconds,
                )
            except RuntimeError as exc:
                doc_id = str(doc_row.get("doc_id", "")).strip() or f"document_{doc_idx}"
                raise RuntimeError(
                    f"Claim extraction failed for {doc_id}; source is not completed"
                ) from exc
        results: list[dict[str, object]] = []
        for item in processed_items:
            anchor_text = (
                str(item.get("citation_evidence_span", "")).strip()
                or str(item.get("raw_claim_text", "")).strip()
            )
            anchor_row = _resolve_document_source_anchor(
                doc_id=str(doc_row.get("doc_id", "")).strip(),
                anchor_text=anchor_text,
                chunk_rows_by_doc_id=chunk_rows_by_doc_id,
            )
            _append_processed_candidates(
                results=results,
                source_row=doc_row,
                processed_items=[item],
                source_type="auto_doc",
                anchor_row=anchor_row,
                review_source_text=extraction_text,
            )
        return results

    doc_args = [
        (i + 1, doc_row, text) for i, (doc_row, text) in enumerate(selected_doc_sources)
    ]
    doc_worker_count = max(
        1, min(int(config.claim_discovery_doc_workers), len(doc_args))
    )
    if doc_args:
        if _step3_async_enabled():

            async def _run_doc_batch() -> list[dict[str, object]]:
                semaphore = asyncio.Semaphore(doc_worker_count)
                gathered = await asyncio.gather(
                    *[_process_doc_async(arg, semaphore) for arg in doc_args]
                )
                rows: list[dict[str, object]] = []
                for batch_rows in gathered:
                    rows.extend(batch_rows)
                return rows

            for row in asyncio.run(_run_doc_batch()):
                raw_claim_index += 1
                row["raw_claim_id"] = f"RC{raw_claim_index:04d}"
                candidates.append(row)
        else:
            with ThreadPoolExecutor(max_workers=doc_worker_count) as executor:
                futures = {executor.submit(_process_doc, arg): arg for arg in doc_args}
                for future in as_completed(futures):
                    for row in future.result():
                        raw_claim_index += 1
                        row["raw_claim_id"] = f"RC{raw_claim_index:04d}"
                        candidates.append(row)
    deduped: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for row in candidates:
        key = (
            str(row["source_type"]),
            str(row["doc_id"]),
            str(row["chunk_id"]),
            " ".join(str(row["raw_claim_text"]).split()),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _require_configured_llm_env(llm_env: dict[str, str] | None) -> dict[str, str]:
    resolved = dict(llm_env or {})
    missing = [
        key for key in _REQUIRED_LLM_ENV_KEYS if not str(resolved.get(key, "")).strip()
    ]
    if missing:
        raise RuntimeError(f"Missing required LLM configuration: {', '.join(missing)}")
    return {key: str(resolved[key]).strip() for key in _REQUIRED_LLM_ENV_KEYS}


def _build_fulltext_by_doc_id(doc_rows: list[dict[str, str]]) -> dict[str, str]:
    fulltext_by_doc_id: dict[str, str] = {}
    for row in doc_rows:
        doc_id = str(row.get("doc_id", "")).strip()
        if not doc_id:
            continue
        fulltext = str(
            row.get("doc_text", "") or row.get("doc_clean_text", "") or ""
        ).strip()
        if fulltext:
            fulltext_by_doc_id[doc_id] = fulltext
    return fulltext_by_doc_id


def _build_chunk_rows_by_doc_id(
    chunk_rows: list[dict[str, str]],
) -> dict[str, list[dict[str, str]]]:
    chunk_rows_by_doc_id: dict[str, list[dict[str, str]]] = {}
    for row in chunk_rows:
        doc_id = str(row.get("doc_id", "")).strip()
        if not doc_id:
            continue
        chunk_rows_by_doc_id.setdefault(doc_id, []).append(row)
    return chunk_rows_by_doc_id


def _resolve_document_source_anchor(
    *,
    doc_id: str,
    anchor_text: str,
    chunk_rows_by_doc_id: dict[str, list[dict[str, str]]],
) -> dict[str, str] | None:
    normalized_anchor = _normalize_model_claim_value(anchor_text)
    if not doc_id:
        return None
    doc_chunks = chunk_rows_by_doc_id.get(doc_id, [])
    if not doc_chunks:
        return None
    if normalized_anchor:
        lowered_anchor = normalized_anchor.lower()
        for row in doc_chunks:
            clean_text = _normalize_model_claim_value(
                str(row.get("clean_text", "") or row.get("text", ""))
            )
            if clean_text and lowered_anchor in clean_text.lower():
                return row
    return None


def _raw_chunk_evidence_scheme(
    chunk: dict[str, object],
    fulltext_by_doc_id: dict[str, str],
    raw_claim_text: str = "",
    citation_numbers: object = "",
    citation_evidence_span: str = "",
) -> dict[str, object]:
    doc_id = str(chunk.get("doc_id", "")).strip()
    scheme = build_raw_reference_scheme(
        chunk_row=chunk,
        fulltext=fulltext_by_doc_id.get(doc_id, ""),
        raw_claim_text=raw_claim_text,
        citation_numbers=citation_numbers,
        citation_evidence_span=citation_evidence_span,
    )
    scheme["paragraph_id"] = str(chunk.get("paragraph_id", "")).strip()
    if scheme.get("citation_numbers"):
        return scheme
    return scheme


def _raw_claim_checkpoint_path(config: PipelineConfig):
    return config.claim_generation_dir / "01_raw_claim_candidates.partial.csv"


def _raw_claim_completed_chunks_path(config: PipelineConfig):
    return config.claim_generation_dir / "01_raw_claim_candidates.completed_chunks.txt"


def _load_raw_claim_checkpoint(
    config: PipelineConfig,
) -> tuple[list[dict[str, object]], set[str]]:
    if checkpoint_journal.path(config).exists():
        return checkpoint_journal.load(config)
    path = _raw_claim_checkpoint_path(config)
    rows = [dict(row) for row in read_csv(path)] if path.exists() else []
    completed_chunk_ids = {
        str(row.get("chunk_id", "")).strip()
        for row in rows
        if str(row.get("source_type", "")).strip() == "auto_chunk"
        and str(row.get("chunk_id", "")).strip()
    }
    completed_path = _raw_claim_completed_chunks_path(config)
    if completed_path.exists():
        completed_chunk_ids.update(
            (
                line.strip()
                for line in completed_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        )
    if rows or completed_chunk_ids:
        _safe_print(
            f"[Stage 4] Resuming from chunk checkpoint: {len(rows)} rows, {len(completed_chunk_ids)} chunks"
        )
    return (rows, completed_chunk_ids)


def _commit_chunk_checkpoint(config, batch_rows, chunk_id):
    checkpoint_journal.commit(config, chunk_id, batch_rows)


def _load_step3a_provider_pool(default_llm_env: dict[str, str]) -> list[dict[str, str]]:
    raw = str(os.getenv("STEP3A_PROVIDER_POOL", "")).strip()
    if not raw:
        return [default_llm_env]
    providers: list[dict[str, str]] = []
    for item in raw.split(";;"):
        parts = [part.strip() for part in item.split("|")]
        if len(parts) < 2 or not parts[0] or (not parts[1]):
            continue
        providers.append(
            {
                "LLM_API_KEY": parts[0],
                "LLM_API_BASE_URL": parts[1],
                "LLM_MODEL_NAME": parts[2]
                if len(parts) >= 3 and parts[2]
                else default_llm_env["LLM_MODEL_NAME"],
            }
        )
    if not providers:
        return [default_llm_env]
    _safe_print(f"[Stage 4] Using {len(providers)} provider(s) for chunk extraction")
    return providers


def _provider_for_chunk(
    provider_pool: list[dict[str, str]], chunk_idx: int
) -> dict[str, str]:
    if not provider_pool:
        raise RuntimeError("provider_pool is empty")
    return provider_pool[(max(1, int(chunk_idx)) - 1) % len(provider_pool)]


def _selected_chunk_sources_cache_path(config: PipelineConfig):
    return config.claim_generation_dir / "01_step3a_selected_chunk_sources.csv"


def _load_selected_chunk_sources_cache(
    config: PipelineConfig,
) -> list[tuple[dict[str, str], str]]:
    path = _selected_chunk_sources_cache_path(config)
    if not path.exists():
        return []
    rows = read_csv(path)
    selected: list[tuple[dict[str, str], str]] = []
    for row in rows:
        extraction_text = str(row.pop("_extraction_text", "")).strip()
        if extraction_text:
            selected.append((dict(row), extraction_text))
    if selected:
        _safe_print(f"[Stage 4] Reusing selected chunk source cache: {len(selected)} chunks")
    return selected


def _write_selected_chunk_sources_cache(
    config: PipelineConfig, selected_chunk_sources: list[tuple[dict[str, str], str]]
) -> None:
    if not selected_chunk_sources:
        return
    rows: list[dict[str, object]] = []
    fieldnames: list[str] = []
    for chunk, extraction_text in selected_chunk_sources:
        row: dict[str, object] = dict(chunk)
        row["_extraction_text"] = extraction_text
        rows.append(row)
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    write_csv(_selected_chunk_sources_cache_path(config), rows, fieldnames)


def _run_integrated_claim_processor(
    llm_env: dict[str, str],
    *,
    config: PipelineConfig,
    text: str,
    scope: str,
    max_claims: int,
    source_context: str,
) -> list[dict[str, str]]:
    return run_llm_entity_integrated_claim_processor(
        llm_env["LLM_API_KEY"],
        llm_env["LLM_API_BASE_URL"],
        llm_env["LLM_MODEL_NAME"],
        text=text,
        scope=scope,
        max_claims=max_claims,
        source_context=source_context,
    )


def _retry_integrated_claim_processor(
    llm_env: dict[str, str],
    *,
    config: PipelineConfig,
    text: str,
    scope: str,
    max_claims: int,
    source_context: str,
    max_attempts: int,
    retry_backoff_seconds: float,
) -> list[dict[str, str]]:
    attempts = max(1, int(max_attempts))
    current_text = text
    last_error: RuntimeError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _run_integrated_claim_processor(
                llm_env,
                config=config,
                text=current_text,
                scope=scope,
                max_claims=max_claims,
                source_context=source_context,
            )
        except RuntimeError as exc:
            last_error = exc
            if attempt >= attempts:
                break
            _safe_print(
                f"[WARN] {scope} entity_integrated claim processor retry {attempt}/{attempts - 1} after provider failure: {exc}"
            )
            if retry_backoff_seconds > 0:
                time.sleep(retry_backoff_seconds * attempt)
    if last_error is not None:
        raise last_error
    raise RuntimeError(
        "entity_integrated claim processor retry failed without a concrete provider error"
    )


async def _retry_integrated_claim_processor_async(
    llm_env: dict[str, str],
    *,
    config: PipelineConfig,
    text: str,
    scope: str,
    max_claims: int,
    source_context: str,
    max_attempts: int,
    retry_backoff_seconds: float,
) -> list[dict[str, str]]:
    attempts = max(1, int(max_attempts))
    current_text = text
    last_error: RuntimeError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await run_llm_entity_integrated_claim_processor_async(
                llm_env["LLM_API_KEY"],
                llm_env["LLM_API_BASE_URL"],
                llm_env["LLM_MODEL_NAME"],
                text=current_text,
                scope=scope,
                max_claims=max_claims,
                source_context=source_context,
            )
        except RuntimeError as exc:
            last_error = exc
            if attempt >= attempts:
                break
            _safe_print(
                f"[WARN] {scope} entity_integrated claim processor retry {attempt}/{attempts - 1} after provider failure: {exc}"
            )
            if retry_backoff_seconds > 0:
                await asyncio.sleep(retry_backoff_seconds * attempt)
    if last_error is not None:
        raise last_error
    raise RuntimeError(
        "entity_integrated claim processor retry failed without a concrete provider error"
    )


def _step3_async_enabled() -> bool:
    raw = str(os.getenv("PIPELINE_STEP3_ASYNC", "1")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _select_claim_discovery_sources(
    rows: list[dict[str, str]],
    *,
    text_field: str,
    fallback_field: str,
    max_sentences: int,
    budget: int,
) -> list[tuple[dict[str, str], str]]:
    sources = []
    for row in rows:
        if row.get("chunk_type") == "reference_entry":
            continue
        if text_field == "clean_text":
            text = str(row.get("text") or row.get(text_field) or "").strip()
        else:
            text = str(row.get(fallback_field) or row.get(text_field) or "").strip()
        if text:
            sources.append((row, text))
    return sources[:budget] if budget > 0 else sources


def _with_doc_context(
    row: dict[str, str], doc_row: dict[str, str] | None
) -> dict[str, str]:
    if not doc_row:
        return dict(row)
    enriched = dict(row)
    for key in ("canonical_title", "inferred_title", "doc_type"):
        if not str(enriched.get(key, "")).strip() and str(doc_row.get(key, "")).strip():
            enriched[key] = str(doc_row.get(key, "")).strip()
    return enriched


def _build_doc_context_by_doc_id(
    *, doc_rows: list[dict[str, str]], chunk_rows: list[dict[str, str]]
) -> dict[str, dict[str, str]]:
    doc_context_by_doc_id: dict[str, dict[str, str]] = {}
    for row in doc_rows:
        doc_id = str(row.get("doc_id", "")).strip()
        if not doc_id:
            continue
        doc_context_by_doc_id[doc_id] = dict(row)
    for row in chunk_rows:
        doc_id = str(row.get("doc_id", "")).strip()
        if not doc_id:
            continue
        merged = doc_context_by_doc_id.get(doc_id, {"doc_id": doc_id})
        merged = _with_doc_context(row, merged)
        doc_context_by_doc_id[doc_id] = merged
    return doc_context_by_doc_id


def _should_keep_extracted_claim(item: dict[str, str]) -> tuple[bool, str]:
    if not str(item.get("raw_claim_text", "")).strip():
        return (False, "empty_claim")
    if str(item.get("keep_or_drop", "uncertain")).strip().lower() != "keep":
        return (False, "model_not_accepted")
    if str(item.get("claim_kind", "")).strip().lower() in {"procedure", "usage"}:
        return (False, "model_procedural_record")
    for key in (
        "source_anchor_valid",
        "teacher_final_claim_ready",
        "guardrail_final_claim_ready",
    ):
        if str(item.get(key, "unknown")).strip().lower() != "true":
            return (False, key + ":not_explicitly_accepted")
    if str(item.get("source_quote_status", "missing")) != "exact":
        return (False, "source_quote_not_found_in_submitted_text")
    project_ready, reason = _project_focus_review(item)
    if project_ready != "true":
        return (False, "project_focus:" + reason)
    recorded_focus = str(item.get("project_focus_ready", "")).strip().lower()
    if recorded_focus and recorded_focus != "true":
        return (False, "project_focus:not_explicitly_accepted")
    if str(item.get("is_atomic", "true")).strip().lower() in {"false", "0", "no"}:
        return (False, "non_atomic")
    return (True, "source_supported_model_assertion")


def _should_skip_raw_candidate(item: dict[str, str]) -> tuple[bool, str]:
    if not str(item.get("raw_claim_text", "")).strip():
        return (True, "empty_claim")
    return (False, "")


def _classify_extracted_claim_with_gate(item: dict[str, str]) -> tuple[str, str]:
    if (
        item.get("teacher_final_claim_ready") == "true"
        and item.get("keep_or_drop") == "keep"
    ):
        return (CONSENSUS_CLAIM, "explicit model acceptance; no lexical promotion")
    return (LOCAL_RESULT, "not accepted by model")


def _apply_extraction_decision(
    item: dict[str, str], *, filter_decision: dict[str, str] | None = None
) -> dict[str, str]:
    filtered = dict(item)
    decision = dict(filter_decision or {})
    raw_claim_text = _normalize_model_claim_value(str(item.get("raw_claim_text", "")))
    filtered["raw_claim_text"] = raw_claim_text
    filtered["extracted_claim_text"] = raw_claim_text
    filtered["source_quote_exact"] = _normalize_model_claim_value(
        str(item.get("source_quote_exact", ""))
    ) or _normalize_model_claim_value(str(item.get("citation_evidence_span", "")))
    filtered["citation_evidence_span"] = (
        _normalize_model_claim_value(str(item.get("citation_evidence_span", "")))
        or filtered["source_quote_exact"]
    )
    filtered["citation_numbers"] = _normalize_citation_numbers(
        item.get("citation_numbers", "")
    )
    filtered["keep_or_drop"] = (
        str(decision.get("keep_or_drop", item.get("keep_or_drop", "keep")))
        .strip()
        .lower()
        or "keep"
    )
    filtered["drop_reason"] = str(
        decision.get("drop_reason", item.get("drop_reason", ""))
    ).strip()
    filtered["claim_kind"] = (
        str(decision.get("claim_kind", item.get("claim_kind", ""))).strip().lower()
    )
    filtered["filter_reasoning"] = str(
        decision.get("filter_reasoning", item.get("filter_reasoning", ""))
    ).strip()
    filtered["knowledge_scope"] = (
        str(decision.get("knowledge_scope", item.get("knowledge_scope", "")))
        .strip()
        .lower()
    )
    filtered["knowledge_scope_reason"] = str(
        decision.get("knowledge_scope_reason", item.get("knowledge_scope_reason", ""))
    ).strip()
    filtered["source_anchor_valid"] = (
        str(decision.get("source_anchor_valid", item.get("source_anchor_valid", "")))
        .strip()
        .lower()
    )
    filtered["entity_relation_summary"] = str(
        decision.get("entity_relation_summary", item.get("entity_relation_summary", ""))
    ).strip()
    filtered["entity_role_freeform"] = str(
        decision.get("entity_role_freeform", item.get("entity_role_freeform", ""))
    ).strip()
    filtered["keep_for_entity_project"] = (
        str(
            decision.get(
                "keep_for_entity_project",
                item.get("keep_for_entity_project", filtered["keep_or_drop"]),
            )
        )
        .strip()
        .lower()
    )
    filtered["keep_reason"] = str(
        decision.get(
            "keep_reason",
            item.get(
                "keep_reason", filtered["filter_reasoning"] or filtered["drop_reason"]
            ),
        )
    ).strip()
    return filtered


def _normalize_citation_numbers(raw_value: object) -> str:
    if isinstance(raw_value, list):
        return ";".join((str(item).strip() for item in raw_value if str(item).strip()))
    text = str(raw_value or "").strip()
    if not text:
        return ""
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if inner:
            parts = [
                part.strip().strip("'\"")
                for part in inner.split(",")
                if part.strip().strip("'\"")
            ]
            return ";".join(parts)
    return ";".join(
        (part.strip() for part in re.split("[;,]\\s*", text) if part.strip())
    )


def _final_candidate_claim_text(item: dict[str, str]) -> str:
    return _normalize_model_claim_value(str(item.get("raw_claim_text", "")))


def _raw_claim_text(item: dict[str, object]) -> str:
    return _normalize_model_claim_value(str(item.get("raw_claim_text", "")))


def _candidate_context_snippet(item: dict[str, object]) -> str:
    context = _normalize_model_claim_value(str(item.get("review_context_3sent", "")))
    if context:
        return context
    parts = [
        _normalize_model_claim_value(str(item.get("prev_sentence_text", ""))),
        _raw_claim_text(item),
        _normalize_model_claim_value(str(item.get("next_sentence_text", ""))),
    ]
    return " ".join((part for part in parts if part)).strip()


def _text_review_context_fields(source_text: str, anchor_text: str) -> dict[str, str]:
    chunk_text = str(source_text or "").strip()
    sentences = _split_chunk_sentences(chunk_text)
    if not sentences:
        return {
            "anchor_sentence_text": anchor_text,
            "prev_sentence_text": "",
            "next_sentence_text": "",
            "review_context_3sent": anchor_text,
        }
    anchor_index = next((i for i, s in enumerate(sentences) if anchor_text in s), 0)
    prev_sentence = sentences[anchor_index - 1] if anchor_index > 0 else ""
    anchor_sentence = sentences[anchor_index]
    next_sentence = (
        sentences[anchor_index + 1] if anchor_index + 1 < len(sentences) else ""
    )
    return {
        "anchor_sentence_text": anchor_sentence,
        "prev_sentence_text": prev_sentence,
        "next_sentence_text": next_sentence,
        "review_context_3sent": " ".join(
            (part for part in (prev_sentence, anchor_sentence, next_sentence) if part)
        ).strip(),
    }


def _split_chunk_sentences(text: str) -> list[str]:
    normalized = re.sub("\\s+", " ", str(text or "")).strip()
    if not normalized:
        return []
    return [
        part.strip() for part in re.split("(?<=[.!?])\\s+", normalized) if part.strip()
    ]


def _with_extraction_source_context(
    item: dict[str, str], source_row: dict[str, str]
) -> dict[str, str]:
    enriched = dict(item)
    for key in (
        "document_domain_fit",
        "document_reaction_fit",
        "doc_id",
        "chunk_id",
        "page_number",
        "canonical_title",
    ):
        value = str(source_row.get(key, "")).strip()
        if value:
            enriched[key] = value
    return enriched


def _build_teacher_source_context(source_row: dict[str, str]) -> str:
    """Supply document metadata once; the complete source text is sent separately."""
    parts = [
        str(
            source_row.get("canonical_title") or source_row.get("inferred_title") or ""
        ).strip(),
        str(source_row.get("doc_type", "")).strip(),
    ]
    return " | ".join((part for part in parts if part))[:1800]


def _prepare_entity_integrated_processed_item(
    item: dict[str, str], source_row: dict[str, str]
) -> dict[str, str]:
    filtered_item = _with_extraction_source_context(
        _apply_extraction_decision(item, filter_decision=item), source_row
    )
    teacher_decision = {
        "teacher_final_claim_ready": str(item.get("teacher_final_claim_ready", ""))
        .strip()
        .lower(),
        "teacher_reject_scope": str(item.get("teacher_reject_scope", ""))
        .strip()
        .lower(),
        "teacher_review_reason": str(item.get("teacher_review_reason", "")).strip(),
    }
    reviewed = _apply_teacher_scope_review(filtered_item, teacher_decision)
    source = str(
        source_row.get("model_source_text")
        or source_row.get("text")
        or source_row.get("clean_text")
        or source_row.get("doc_clean_text")
        or source_row.get("doc_text")
        or ""
    )
    quote = str(
        item.get("source_quote_exact") or item.get("citation_evidence_span") or ""
    ).strip()
    reviewed["source_quote_exact"] = quote
    reviewed["model_source_anchor_valid"] = str(
        item.get("source_anchor_valid", "unknown")
    )
    reviewed["source_anchor_valid"] = str(
        item.get("source_anchor_valid", "unknown")
    ).lower()
    reviewed["source_quote_status"] = (
        "exact"
        if quote and quote in source
        else "missing"
        if not quote
        else "unmatched"
    )
    reviewed["source_quote_start"] = source.find(quote) if quote else -1
    reviewed["source_quote_end"] = (
        source.find(quote) + len(quote) if quote and quote in source else -1
    )
    return reviewed


def _apply_teacher_scope_review(
    item: dict[str, str], teacher_decision: dict[str, str]
) -> dict[str, str]:
    reviewed = dict(item)
    reviewed["teacher_final_claim_ready"] = (
        str(
            teacher_decision.get(
                "teacher_final_claim_ready", item.get("teacher_final_claim_ready", "")
            )
        )
        .strip()
        .lower()
    )
    reviewed["teacher_reject_scope"] = (
        str(
            teacher_decision.get(
                "teacher_reject_scope", item.get("teacher_reject_scope", "")
            )
        )
        .strip()
        .lower()
    )
    reviewed["teacher_review_reason"] = str(
        teacher_decision.get(
            "teacher_review_reason", item.get("teacher_review_reason", "")
        )
    ).strip()
    return _apply_teacher_scope_guard(reviewed)


def _apply_teacher_scope_guard(item: dict[str, str]) -> dict[str, str]:
    reviewed = dict(item)
    decision = str(item.get("teacher_final_claim_ready", "unknown")).strip().lower()
    if decision not in ("true", "false"):
        decision = "unknown"
    reviewed["teacher_final_claim_ready"] = decision
    reviewed["guardrail_final_claim_ready"] = decision
    reviewed["guardrail_reason"] = (
        "preserve_explicit_model_decision; no category override"
    )
    return reviewed


def _apply_project_focus_gate(item: dict[str, str]) -> dict[str, str]:
    focused = dict(item)
    ready, reason = _project_focus_review(focused)
    focused["project_focus_ready"] = ready
    focused["project_focus_reason"] = reason
    return focused


def _project_focus_review(item: dict[str, object]) -> tuple[str, str]:
    if not str(item.get("raw_claim_text", "")).strip():
        return ("false", "empty_claim")
    value = str(item.get("keep_for_entity_project", "uncertain")).lower().strip()
    return (
        "true" if value == "keep" else "false" if value == "drop" else "unknown",
        str(item.get("keep_reason", "model entity relevance decision")),
    )


def _normalize_model_claim_value(text: str) -> str:
    return re.sub("\\s+", " ", text.strip())


def _safe_print(message: str) -> None:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        print(message)
    except UnicodeEncodeError:
        sanitized = message.encode(encoding, errors="replace").decode(
            encoding, errors="replace"
        )
        print(sanitized)


def _pack_claim_sources(sources, *, max_chunks, max_chars):
    """Pack contiguous sources from one document, never truncate or filter text."""
    if max_chunks < 1 or max_chars < 1:
        raise ValueError("Extraction batch limits must be positive")
    batch, size, seen = [], 0, set()
    for row, text in sources:
        chunk_id = str(row.get("chunk_id", "")).strip()
        if not chunk_id or chunk_id in seen:
            raise ValueError("Every extraction source needs a unique nonempty chunk_id")
        seen.add(chunk_id)
        if batch and (
            row.get("doc_id") != batch[0][0].get("doc_id")
            or len(batch) >= max_chunks
            or size + len(text) > max_chars
        ):
            yield batch
            batch, size = [], 0
        batch.append((row, text))
        size += len(text)
    if batch:
        yield batch
