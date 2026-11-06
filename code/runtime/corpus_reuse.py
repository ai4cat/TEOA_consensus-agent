"""Explicit copying of completed preprocessing; no content verification."""

import shutil
from pathlib import Path
from runtime.artifacts import read, validate
from runtime.registry import STAGE_LAYOUT


def reuse_corpus(ctx):
    supplied = ctx.config.get("corpus_run")
    if not supplied:
        return False
    source = Path(supplied).resolve()
    destination = ctx.run.resolve()
    if (
        source == destination
        or source.is_relative_to(destination)
        or destination.is_relative_to(source)
    ):
        raise ValueError("The corpus source must be a separate run directory")
    config = read(source / "state/run_config.json")
    if config.get("stage_layout") != STAGE_LAYOUT:
        raise ValueError(
            "Corpus reuse requires Stage 1 from the linear-stages.v2 layout; use an explicit migration for older runs"
        )
    from runtime.engine import validate_stage

    receipt = validate_stage(source, 1, config["identity"])
    origin = source / "outputs/01_corpus"
    previous = (
        read(origin / "corpus_reuse_provenance.json")
        if (origin / "corpus_reuse_provenance.json").exists()
        else None
    )
    for name in receipt["files"]:
        ctx.controller.check()
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe corpus artifact path")
        src = origin / relative
        if src.is_symlink() or not src.resolve().is_relative_to(origin.resolve()):
            raise ValueError(
                "Corpus artifacts must stay inside the source output directory"
            )
        dst = ctx.pending / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    validate(ctx.pending, receipt["files"])
    validate(origin, receipt["files"])
    ctx.emit(
        "corpus_reuse_provenance.json",
        {
            "kind": "explicit-corpus-copy.v2",
            "source_run": str(source),
            "source_receipt": receipt,
            "source_artifact_inventory": receipt["files"],
            "previous_reuse_provenance": previous,
            "preprocessing_reexecuted": False,
            "model_responses_imported": False,
        },
    )
    ctx.status(
        state="corpus_reused", files=len(receipt["files"]), source_run=str(source)
    )
    return True
