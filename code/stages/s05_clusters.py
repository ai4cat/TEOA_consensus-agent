from claims import clustering as c

def run(ctx):
    ctx.setup_models();ctx.hydrate(4)
    c._run_step3b(ctx.context,ctx.raw)
    for p in sorted(ctx.cfg.claim_generation_dir.glob('*.csv')):
        if not ctx.upstream(4,p.name).exists():ctx.publish(p)
