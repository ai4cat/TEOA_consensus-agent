import shutil
from pathlib import Path


def run(ctx):
    ctx.controller.check()
    if ctx.config.get("candidate_cache_run"):
        from preparation.candidate_reuse import reuse_candidates
        reuse_candidates(ctx, ctx.config["candidate_cache_run"])
        return
    from preparation import nomination as nomination_stage
    from functools import partial
    from runtime.transport import NominationCaller

    original = nomination_stage.ResilientCaller
    nomination_stage.ResilientCaller = partial(
        NominationCaller, controller=ctx.controller
    )
    try:
        value = nomination_stage.nominate(
            ctx.work,
            model=ctx.config["candidate_model"],
            base=ctx.config["base_url"],
            credential_pool=ctx.config.get("credential_pool"),
        )
    finally:
        nomination_stage.ResilientCaller = original
    ctx.publish(ctx.work / "nomination/candidates.json")
    shutil.copytree(
        ctx.work / "nomination/model_calls",
        ctx.communications / "original",
        dirs_exist_ok=True,
    )
    calls = ctx.work / "nomination/model_calls"
    accepted = Path(value["accepted_attempt"]).relative_to(calls).as_posix()
    ctx.emit(
        "nomination_provenance.json",
        {
            "mode": "online",
            "model": ctx.config["candidate_model"],
            "prompt_input": "inputs/01_prompts/prompt.txt",
            "candidate_output": "outputs/02_candidates/candidates.json",
            "communications_root": "communications/02_candidates/original",
            "accepted_attempt": "communications/02_candidates/original/" + accepted,
            "candidate_count": value["candidate_count"],
            "calls_this_invocation": value["calls_this_invocation"],
        },
    )
