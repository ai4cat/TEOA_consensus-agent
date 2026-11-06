"""Stage4 source-grounded raw-claim extraction with model scope decisions."""

from runtime.artifacts import table
from claims import clustering as c


def run(ctx):
    ctx.setup_models()
    raw = c._run_step3a(ctx.context, reuse_raw_candidates=False)
    for index, row in enumerate(raw, 1):
        row["raw_claim_id"] = f"RC_{index:08d}"
        row.pop("entered_structured_pool", None)
    if len({r["raw_claim_id"] for r in raw}) != len(raw):
        raise ValueError("Raw identity collision")
    table(ctx.cfg.raw_claim_candidates_path, raw)
    c._write_claim_scope_review_artifacts(ctx.cfg, raw)
    for p in sorted(ctx.cfg.claim_generation_dir.glob("*.csv")):
        ctx.publish(p)
    ctx.publish(ctx.cfg.claim_generation_dir / "source_chunk_selection.json")
