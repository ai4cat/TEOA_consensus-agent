from claims import clustering as c

def run(ctx):
    ctx.hydrate(4);ctx.hydrate(5)
    c._run_step3c(ctx.context,ctx.structured)
    ctx.publish(ctx.cfg.step3c_generalization_review_path)
    ctx.publish(ctx.cfg.support_evidence_rows_path)
