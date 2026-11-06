from shared.storage import read_csv
from claims import clustering as c
from runtime.artifacts import table

def run(ctx):
    cfg=ctx.cfg;context=ctx.context;run=ctx.work;raw=ctx.raw;structured=ctx.structured
    for number in (4,5,6):ctx.hydrate(number)
    c._run_step3d(context,read_csv(cfg.step3c_generalization_review_path),read_csv(cfg.support_evidence_rows_path))
    themes=read_csv(cfg.consensus_claims_path)
    pm={i.strip() for r in structured for i in r['member_raw_claim_ids'].split(';') if i.strip()}
    tm={i.strip() for r in themes for i in r['source_raw_claim_ids'].split(';') if i.strip()}
    judges={r['raw_claim_id']:r for r in read_csv(cfg.claim_generation_dir/'03B_cluster_candidate_review.csv')}
    membership=[{'raw_claim_id':r['raw_claim_id'],'stage3a_accepted':r.get('stage3a_accepted','unknown'),
        'stage3b_cluster_ready':judges.get(r['raw_claim_id'],{}).get('cluster_ready','not_submitted'),
        'current_structured_pool_member':str(r['raw_claim_id'] in pm).lower(),
        'current_final_theme_member':str(r['raw_claim_id'] in tm).lower()} for r in raw]
    table(run/'stage_membership.csv',membership)
    assert not (tm-pm) and all(judges[i]['cluster_ready']=='true' for i in pm)
    assert all(r['stage3a_accepted']=='true' for r in membership if r['current_structured_pool_member']=='true')

    for p in sorted(cfg.claim_generation_dir.glob('*.csv')):
        if not any(ctx.upstream(n,p.name).exists() for n in (4,5,6)):ctx.publish(p)
    ctx.publish(run/'stage_membership.csv')
