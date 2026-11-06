"""Build a portable package from the stage deliverables."""

import os, shutil, uuid, time
from runtime.artifacts import read, save, inventory, validate, fs_path
from runtime.registry import folder
from shared.storage import read_csv

DELIVERABLES = {
    1: ('fulltext', 'body_paragraphs.csv', 'chunks.csv', 'document_abstracts.csv',
        'documents.csv', 'source_index.json', 'summary.json'),
    2: ('candidates.json',),
    3: ('entity_ranking.csv', 'entity_recount.json', 'entity_selection.json'),
    4: ('01_raw_claim_candidates.csv', '01_step3a_selected_chunk_sources.csv',
        '03A_claim_scope_review.csv', '03A_quarantined_candidates.csv', 'source_chunk_selection.json'),
    5: ('02_structured_claim_pool.csv', '03B_cluster_candidate_review.csv',
        '03B_cluster_quality_audit.csv', '03B_consensus_verifier_review.csv'),
    6: ('01_support_evidence_rows.csv', '03C_generalization_review.csv'),
    7: ('03_claims_master_all.csv', '03_claims_master.csv', '04_consensus_claims_all.csv',
        '04_consensus_claims.csv', '04A_consensus_hygiene_review.csv', 'stage_membership.csv'),
    8: ('document_summaries.csv', 'summary_scope.json'),
    9: ('reference_documents', 'reference_sources', 'reference_acquisition.csv',
        'claim_document_links.csv', 'collection_decisions.csv', 'library_summary.json',
        'reference_fulltext_links.csv', 'reference_inventory.csv', 'selected_documents.csv',
        'source_catalog.csv', 'summary.json', 'unavailable_references.csv'),
    10: ('01_raw_claim_candidates_scores.csv', 'missing_author_abstracts.json',
         'reference_mapping.csv', 'scoring_scope.json'),
    11: ('selected_claim.json', 'sensitivity.csv', 'themes_ranking.csv', 'topk_overlap.csv'),
    12: ('extractions', 'input_tasks', 'statistics'),
}


def validate_science(ctx):
    from runtime.library import validate_library
    from runtime.scoring_scope import select_scoring_claims, POLICY

    library_validation = validate_library(ctx)
    raw = read_csv(ctx.upstream(4, "01_raw_claim_candidates.csv"))
    scores = read_csv(ctx.upstream(10, "01_raw_claim_candidates_scores.csv"))
    themes = read_csv(ctx.upstream(7, "04_consensus_claims.csv"))
    raw_ids = [r["raw_claim_id"] for r in raw]
    scoring_ids = {r['raw_claim_id'] for r in select_scoring_claims(raw, themes)}
    score_ids = [r["raw_claim_id"] for r in scores]
    if (
        len(set(raw_ids)) != len(raw_ids)
        or len(set(score_ids)) != len(score_ids)
        or scoring_ids != set(score_ids)
    ):
        raise ValueError("Final-theme member claims and scores must be one-to-one")
    members = read_csv(ctx.upstream(7, "stage_membership.csv"))
    if {r["raw_claim_id"] for r in members} != set(raw_ids):
        raise ValueError("Membership coverage differs")
    if {r['raw_claim_id'] for r in members if r['current_final_theme_member'] == 'true'} != scoring_ids:
        raise ValueError('Membership flags differ from final-theme scoring scope')
    if any(
        (
            r["current_final_theme_member"] == "true"
            and (
                r["current_structured_pool_member"] != "true"
                or r["stage3a_accepted"] != "true"
                or r["stage3b_cluster_ready"] != "true"
            )
            for r in members
        )
    ):
        raise ValueError("Rejected claim entered a final theme")
    from runtime.platform_library import validate_platform_results
    platform = validate_platform_results(ctx)
    return {
        "state": "passed",
        "raw_claims": len(raw),
        "scores": len(scores),
        "themes": len(themes),
        "system_parts": platform["tasks"],
        "scoring_scope": POLICY,
        "scoped_claims": len(scoring_ids),
        "all_scoped_ids_scored": True,
        "unscored_out_of_scope": len(raw_ids) - len(scoring_ids),
        "negative_model_members_in_final": 0,
        "systems_fully_processed_documents": platform["documents"],
        "platform_scope": platform,
        "library_membership": library_validation,
        "mode": ctx.mode,
    }


def copy_project_files(root, destination):
    """Preserve the runnable root entry points and their code/ targets."""
    shutil.copytree(root / "code", destination / "code",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("agent.cmd", "agent.ps1", "stage.ps1", "setup.cmd",
                 "setup.ps1", "requirements.txt", "README.md", "LICENSE", ".gitignore",
                 ".gitattributes"):
        shutil.copy2(root / name, destination / name)
    for name in ("docs", "tests"):
        shutil.copytree(root / name, destination / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def run(ctx):
    validation = validate_science(ctx)
    ctx.emit("validation.json", validation)
    ctx.emit(
        "output_inventory.json",
        {folder(n): inventory(ctx.run / "outputs" / folder(n)) for n in range(1, 13)},
    )
    destination = ctx.run / "delivery"
    prepared = ctx.work / "delivery_prepared.json"
    if destination.exists():
        if not prepared.exists():
            raise ValueError("Existing delivery has no prepared commit")
        validate(destination, read(prepared)["files"])
        ctx.emit("delivery.json", {"state": "completed", "path": "delivery"})
        return
    staging = fs_path(ctx.work / ("delivery_build_" + uuid.uuid4().hex))
    staging.mkdir()
    copy_project_files(ctx.root, staging)
    shutil.copytree(ctx.run / "communications", staging / "communications")
    parameter_keys = (
        "mode", "stage_layout", "identity", "candidate_model", "base_url",
        "initial_concurrency", "max_concurrency", "corpus_run", "candidate_cache_run",
    )
    save(staging / "run_parameters.json", {
        key: ctx.config[key] for key in parameter_keys if key in ctx.config
    })
    parameter_path = ctx.run / "state/stage_parameters.json"
    if parameter_path.is_file():
        shutil.copy2(parameter_path, staging / "stage_parameters.json")
    save(staging / "stage_receipts.json", {
        folder(n): read(ctx.run / "state" / folder(n) / "completed.json")
        for n in range(1, 13)
    })
    shutil.copytree(
        ctx.root / "inputs", staging / "inputs",
        ignore=shutil.ignore_patterns("agent_library"),
    )
    from runtime.agent_contract import agent_state, stage_contract, LIBRARY
    from runtime.online_references import POLICY as online_policy
    online = validation['library_membership']['policy'] == online_policy

    if agent_state(ctx.run) is not None:
        stage_contract(ctx.run, 13)
        if online:
            # Runtime snapshots bind approvals; the portable delivery needs one
            # canonical copy. Check equality before collapsing those paths.
            snapshot = ctx.run / LIBRARY / '02_documents'
            for row in read_csv(snapshot / 'catalog.csv'):
                if not row.get('discovery_doc_ids'):
                    continue
                relative = row['relative_path']
                current = (staging / 'inputs/02_documents' / relative).resolve()
                original = (snapshot / relative).resolve()
                if (not current.is_relative_to((staging / 'inputs/02_documents').resolve())
                        or not original.is_relative_to(snapshot.resolve())):
                    raise ValueError('Invalid primary document path')
                if current.read_bytes() != original.read_bytes():
                    raise ValueError('Primary input differs from the approved runtime snapshot')
        else:
            shutil.copytree(
                ctx.run / LIBRARY, staging / LIBRARY,
                ignore=shutil.ignore_patterns("manifest.json"),
            )
    if online:
        from runtime.artifacts import table
        # Online reference originals are Stage 9 outputs. Only the primary corpus
        # belongs in these input catalogs; the separate platform library is kept.
        for documents in (staging / 'inputs/02_documents', staging / LIBRARY / '02_documents'):
            if not documents.exists():
                continue
            catalog_path = documents / 'catalog.csv'
            rows = read_csv(catalog_path)
            for row in rows:
                if row.get('discovery_doc_ids'):
                    continue
                path = (documents / row['relative_path']).resolve()
                if not path.is_relative_to(documents.resolve()):
                    raise ValueError('Invalid delivery document path')
                path.unlink()
            table(catalog_path, [r for r in rows if r.get('discovery_doc_ids')], list(rows[0]) if rows else None)
    for n in range(1, 13):
        destination_stage = staging / 'outputs' / folder(n)
        destination_stage.mkdir(parents=True)
        for name in DELIVERABLES[n]:
            source = ctx.run / 'outputs' / folder(n) / name
            if source.is_dir():
                shutil.copytree(source, destination_stage / name)
            elif source.is_file():
                shutil.copy2(source, destination_stage / name)
    if online:
        # Keep delivered source assignments aligned with the single input copy.
        for name, column in [('source_catalog.csv', 'input_prefix'),
                             ('selected_documents.csv', 'source_path')]:
            path = staging / 'outputs/09_library' / name
            rows = read_csv(path)
            for row in rows:
                value = row.get(column, '')
                prefix = LIBRARY + '/02_documents'
                if value == prefix or value.startswith(prefix + '/'):
                    row[column] = 'inputs/02_documents' + value[len(prefix):]
            table(path, rows, list(rows[0]) if rows else None)
    # The request definition is included with its original communications.
    manifest_path = staging / "outputs/12_systems/extractions/manifest.json"
    manifest = read(manifest_path)
    definition = manifest.get("request_definition")
    if definition and not (staging / definition).is_file():
        raise ValueError("Delivered platform request definition is missing")
    save(manifest_path, manifest)
    save(prepared, {"files": inventory(staging)})
    ctx.controller.check()
    for attempt in range(20):
        if destination.exists():
            raise FileExistsError("Delivery appeared during publication")
        try:
            os.rename(staging, fs_path(destination))
            break
        except PermissionError as exc:
            if (
                os.name != "nt"
                or getattr(exc, "winerror", None) not in (5, 32, 33)
                or attempt == 19
            ):
                raise
            time.sleep(min(0.025 * (attempt + 1), 0.25))
    ctx.emit("delivery.json", {"state": "completed", "path": "delivery"})
