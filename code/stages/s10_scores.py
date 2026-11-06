import json
from types import SimpleNamespace
from concurrent.futures import as_completed
from runtime.concurrency import AdaptiveExecutor
from shared.storage import read_csv
from scoring import scorer,evidence,impact
from runtime.artifacts import save as write,table

def run(ctx):
    from runtime.library import validate_library
    from runtime.online_references import POLICY
    if validate_library(ctx)['policy'] != POLICY:
        raise ValueError('Stage 9 predates online article acquisition. Use a new run for online reference scoring.')
    raw=ctx.scoring_raw;sourcepaths=ctx.sourcepaths;run=ctx.work;chunks=ctx.chunks
    from runtime.scoring_scope import describe_scope
    if not raw:raise ValueError('No final-theme claims available for scoring')
    ctx.emit('scoring_scope.json',describe_scope(raw))
    args=SimpleNamespace(reference_parsed=ctx.upstream(9,'reference_documents'))
    transport,llm_env=ctx.setup_models();status=ctx.status
    inventory=read_csv(sourcepaths['references']);doi_map=scorer._index_dois_by_raw_claim(inventory,raw)
    refmap=[{'raw_claim_id':r['raw_claim_id'],'doc_id':r['doc_id'],
             'resolved_dois':json.dumps(doi_map.get(r['raw_claim_id'],[])),
             'reference_used':r.get('reference_used','[]'),'status':'resolved_from_current_run_reference_inventory' if doi_map.get(r['raw_claim_id']) else 'no_resolved_reference'} for r in raw]
    table(run/'reference_mapping.csv',refmap)
    abstracts=scorer._index_abstracts(sourcepaths['abstracts'])
    summaries=scorer._index_structured_summaries(sourcepaths['summaries'])
    missing_summary={r['doc_id'] for r in raw}-set(summaries)
    if missing_summary:raise ValueError('Structured summaries missing: '+str(sorted(missing_summary)))
    write(run/'missing_author_abstracts.json',{'policy':'missing author abstracts explicitly allowed; never replaced by generated abstracts',
        'raw_claim_ids':[r['raw_claim_id'] for r in raw if not abstracts.get(r['doc_id'])]})
    chunk_by={r['chunk_id']:r.get('text','') for r in chunks}
    score_path=run/'scores/01_raw_claim_candidates_scores.csv'
    def scoring_action():
        transport.set_stage('audit')
        completed=[]
        def one(row):
            text=chunk_by.get(row.get('chunk_id'),row.get('citation_evidence_span',''))
            primary=evidence._score_evidence_primary(claim_text=row['raw_claim_text'],anchor_text=row.get('source_quote_exact',''),chunk_text=text,llm_env=llm_env)
            reference=evidence._score_evidence_reference(claim_text=row['raw_claim_text'],ref_dois=doi_map.get(row['raw_claim_id'],[]),reference_root=args.reference_parsed,llm_env=llm_env)
            importance=impact._score_impact(claim_text=row['raw_claim_text'],chunk_text=text,
                abstract_text=abstracts.get(row['doc_id'],''),structured_summary=summaries[row['doc_id']],llm_env=llm_env)
            a=primary.get('evidence_score');b=(reference or {}).get('evidence_score');i=importance.get('impact_score')
            if type(a) is not int or not 0<=a<=3 or type(i) is not int or not 0<=i<=4:raise ValueError('Incomplete score '+row['raw_claim_id'])
            if reference and (type(b) is not int or not 0<=b<=3):raise ValueError('Incomplete reference score')
            final=max(a,b) if b is not None else a
            origin='primary_only' if b is None else 'tied' if a==b else 'reference_max' if b>a else 'primary_max'
            return scorer._build_output_row(claim=row,primary=primary,reference=reference,impact=importance,
                final_score=str(final),final_source=origin,ref_dois=doi_map.get(row['raw_claim_id'],[]))
        errors=[]
        with AdaptiveExecutor(max_workers=transport.maximum,transport=transport,controller=ctx.controller) as pool:
            pending={pool.submit(one,r):r for r in raw}
            for future in as_completed(pending):
                try:completed.append(future.result())
                except Exception as exc:errors.append({'raw_claim_id':pending[future]['raw_claim_id'],'error_type':type(exc).__name__,'message':str(exc)})
                if len(completed)%25==0 or len(completed)+len(errors)==len(raw):
                    table(score_path,sorted(completed,key=lambda r:r['raw_claim_id']),scorer.OUTPUT_FIELDS if hasattr(scorer,'OUTPUT_FIELDS') else None)
                    status(state='audit',scores_completed=len(completed),scores_total=len(raw),score_errors=len(errors))
        write(run/'score_errors.json',errors)
        if errors:raise RuntimeError('Incomplete scoring; successful request caches preserved')
        return [score_path]

    scoring_action()
    # Timings are runtime telemetry, not scientific score values.
    rows=read_csv(score_path)
    for row in rows:
        for key in ('primary_elapsed_s','reference_elapsed_s','impact_elapsed_s'):row.pop(key,None)
    table(ctx.pending/score_path.name,rows)
    ctx.publish(run/'reference_mapping.csv');ctx.publish(run/'missing_author_abstracts.json')
