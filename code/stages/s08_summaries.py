"""Stage8 document summaries; checkpoints are local to this run."""

from collections import defaultdict
from concurrent.futures import as_completed
import json
from runtime.concurrency import AdaptiveExecutor
from claims import abstraction as ab
from runtime.artifacts import read, save, table
from runtime.scoring_scope import describe_scope


def run(ctx):
    scope = describe_scope(ctx.scoring_raw)
    selected_docs = set(scope['doc_ids'])
    available_docs = {r['doc_id'] for r in ctx.docs}
    if selected_docs - available_docs:
        raise ValueError('Scoring source documents are missing from the corpus')
    grouped = defaultdict(list)
    for row in ctx.chunks:
        if row['doc_id'] in selected_docs:
            grouped[row['doc_id']].append(row)
    if any(not ab._select_document_chunks(grouped[d]) for d in selected_docs):
        raise ValueError('Scoring source document has no readable summary chunks')
    ctx.emit('summary_scope.json', {**scope, 'source_paper_count': len(selected_docs),
                                  'source_claim_count': len(scope['raw_claim_ids']),
                                  'corpus_document_count': len(available_docs)})
    if not selected_docs:
        table(ctx.pending / 'document_summaries.csv', [], ab.DOCUMENT_ABSTRACTION_FIELDS)
        return
    transport, env = ctx.setup_models()
    ab._request_responses_api = transport.request
    ab._load_document_abstraction_llm_env = lambda: dict(env)
    checkpoints = ctx.work / "summaries"
    checkpoints.mkdir(exist_ok=True)

    def one(doc_id):
        ctx.controller.check()
        rows = grouped[doc_id]
        path = checkpoints / (doc_id + ".json")
        source_input = {
            'model': env['LLM_MODEL_NAME'],
            'max_output_tokens': ab._DEFAULT_DOCUMENT_ABSTRACTION_MAX_OUTPUT_TOKENS,
            'base_url': env['LLM_API_BASE_URL'],
            'prompt': ab._build_document_abstraction_prompt(doc_id, ab._select_document_chunks(rows)),
        }
        if path.exists():
            saved = read(path)
            if saved.get('input') == source_input:
                validate_row(saved['row'], doc_id, rows)
                return saved['row'], True
        value = ab.build_document_abstraction_row(doc_id, rows)
        validate_row(value, doc_id, rows)
        save(path, {"input": source_input, "row": value})
        return value, False

    values = []
    errors = []
    reused = 0
    with AdaptiveExecutor(
        max_workers=transport.maximum, transport=transport, controller=ctx.controller
    ) as pool:
        futures = {pool.submit(one, doc_id): doc_id for doc_id in sorted(selected_docs)}
        for future in as_completed(futures):
            try:
                value, was_reused = future.result()
                values.append(value)
                reused += int(was_reused)
            except Exception as exc:
                errors.append({'doc_id': futures[future], 'error_type': type(exc).__name__, 'message': str(exc)})
            ctx.status(state="summaries", completed=len(values), total=len(futures),
                       failed=len(errors), reused=reused)
    save(ctx.work/'summary_errors.json', errors)
    if errors:
        raise RuntimeError(f'{len(errors)} source summaries failed; successful source checkpoints preserved')
    table(
        ctx.pending / "document_summaries.csv",
        sorted(values, key=lambda x: x["doc_id"]),
        ab.DOCUMENT_ABSTRACTION_FIELDS,
    )


def validate_row(row, doc_id, chunks):
    if row.get('doc_id') != doc_id or any(field not in row for field in ab.DOCUMENT_ABSTRACTION_FIELDS):
        raise ValueError('Invalid document summary schema or identity')
    if not any(str(row[field]).strip() for field in ab.DOCUMENT_ABSTRACTION_FIELDS if field.endswith('_summary')):
        raise ValueError('Document summary has no supported content')
    from shared.selected_entity import MATCH_TOKEN, normalize_entity

    target_role = str(row['target_role_summary']).strip()
    if target_role and MATCH_TOKEN not in normalize_entity(target_role):
        raise ValueError('Target-role summary must name the upstream-selected entity or a supplied alias')
    supporting = json.loads(row['supporting_chunk_ids_json'])
    allowed = {r['chunk_id'] for r in ab._select_document_chunks(chunks)}
    if not isinstance(supporting, list) or not supporting or any(i not in allowed for i in supporting):
        raise ValueError('Document summary lacks valid supporting chunks')
