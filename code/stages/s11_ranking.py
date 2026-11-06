from shared.storage import read_csv
from selection.ranking import reconstruct_v3, sensitivity
from runtime.artifacts import table


def run(ctx):
    scores = read_csv(ctx.upstream(10, "01_raw_claim_candidates_scores.csv"))
    ranked = reconstruct_v3(ctx.themes, ctx.raw, scores)
    sens, overlap = sensitivity(ranked)
    for data in (ranked, sens, overlap):
        for row in data:
            row.update(run_mode="entity_workflow_v2", scoring_mode="model_execution")
    for name, data in [
        ("themes_ranking.csv", ranked),
        ("sensitivity.csv", sens),
        ("topk_overlap.csv", overlap),
    ]:
        table(ctx.pending / name, data)
    ctx.emit("selected_claim.json", ranked[0])
