"""The claim-linked document store for reference evidence scoring.

No topic, chemical name, historical batch or target document count is a gate.
Only explicit source/citation links enter the collection. Missing fulltext is
reported separately. Platform analysis uses its own independent Lib2 collection.
"""

import re, unicodedata
from collections import defaultdict
from pathlib import Path
from runtime.artifacts import read, save, table
from shared.storage import read_csv
from runtime.curated_metadata import source_metadata
from preparation.parsing.content_list import normalize_content_list

POLICY = "final-theme-source-and-cited-fulltext-v1"


def ids(value):
    return {x.strip() for x in str(value or "").split(";") if x.strip()}


def doi_key(value):
    value = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    value = re.sub("^(?:https?://(?:dx\\.)?doi\\.org/|doi:\\s*)", "", value)
    return value if re.fullmatch("10\\.\\d{4,9}/\\S+", value) else ""


def catalog(root, document_folder=None):
    folder = (
        Path(document_folder)
        if document_folder is not None
        else Path(root) / "inputs/02_documents"
    )
    rows = read_csv(folder / "catalog.csv")
    seen = set()
    discovery = set()
    for row in rows:
        if row["source_id"] in seen:
            raise ValueError("Duplicate source identity")
        seen.add(row["source_id"])
        path = (folder / row["relative_path"]).resolve()
        if not path.is_relative_to(folder.resolve()) or not path.is_file():
            raise ValueError("Invalid source path")
        aliases = ids(row["discovery_doc_ids"])
        if discovery & aliases:
            raise ValueError("Duplicate discovery document identity")
        discovery.update(aliases)
    actual = {p.relative_to(folder).as_posix() for p in folder.rglob("*.json")}
    if actual != {r["relative_path"] for r in rows}:
        raise ValueError("Uncatalogued or missing document JSON")
    return rows


def source_blocks(payload):
    if (
        isinstance(payload, list)
        and payload
        and all((isinstance(p, list) for p in payload))
    ):
        from stages.s01_corpus import normalize_system_source

        return normalize_system_source(payload)
    flat = normalize_content_list(payload)
    if not isinstance(flat, list):
        raise ValueError("Unsupported document JSON shape")
    result = []
    for index, block in enumerate(flat):
        if not isinstance(block, dict):
            continue
        kind = block.get("type", "")
        if kind not in ("text", "paragraph", "title", "list", "ref_text"):
            continue
        text = block.get("text", "")
        if kind == "list" and (not text):
            text = " ".join((str(x) for x in block.get("list_items", [])))
        if str(text).strip():
            result.append(
                {
                    "type": "text",
                    "text": str(text),
                    "page_idx": int(block.get("page_idx", 0)),
                    "original_block_type": kind,
                    "original_page_block_index": index,
                    "bbox": block.get("bbox"),
                }
            )
    return result


def index_sources(ctx):
    result = []
    work = ctx.work / "source_index"
    work.mkdir(exist_ok=True)
    for count, row in enumerate(catalog(ctx.root, ctx.source("02_documents")), 1):
        if not row.get("discovery_doc_ids"):
            continue
        ctx.controller.check()
        path = ctx.source("02_documents") / row["relative_path"]
        checkpoint = work / (row["source_id"] + ".json")
        payload = read(path)
        meta = source_metadata(ctx.root, path, payload)
        # Old run snapshots retain their original column names; new catalogs use doi.
        declared = doi_key(row.get("doi", row.get("doi_hint", "")))
        extracted = doi_key(meta.get("doi"))
        conflict = bool(declared and extracted and (declared != extracted))
        doi = "" if conflict else declared or extracted
        blocks = source_blocks(payload)
        value = {
            **row,
            "doi": doi,
            "extracted_doi": extracted,
            "declared_doi": declared,
            "doi_conflict": conflict,
            "title": meta.get("title", ""),
            "has_text": bool(blocks),
        }
        save(checkpoint, value)
        result.append(value)
        if count % 100 == 0:
            ctx.status(state="indexing_library", completed=count)
    return result


def group_sources(sources):
    """Group catalog sources by unambiguous DOI or declared source identity."""
    by_key = {}
    groups = {}
    alias = {}
    by_doi = {}
    for row in sorted(sources, key=lambda r: r["source_id"]):
        key = "doi:" + row["doi"] if row["doi"] else "source:" + row["source_id"]
        by_key.setdefault(key, []).append(row)
    for index, (key, variants) in enumerate(sorted(by_key.items()), 1):
        ident = f"DOC_{index:08d}"
        chosen = min(
            variants,
            key=lambda r: (
                not has_text(r),
                not bool(r["discovery_doc_ids"]),
                r["source_id"],
            ),
        )
        group = {
            "doc_id": ident,
            "identity_key": key,
            "doi": chosen["doi"],
            "chosen": chosen,
            "variants": variants,
        }
        groups[ident] = group
        for row in variants:
            for name in ids(row["discovery_doc_ids"]):
                alias[name] = ident
        if chosen["doi"]:
            by_doi[chosen["doi"]] = ident
    return (groups, alias, by_doi)


def has_text(row):
    return row["has_text"] is True or str(row["has_text"]).lower() == "true"


def select_documents(themes, raw, references, sources, available_reference_dois=None):
    groups, alias, by_doi = group_sources(sources)
    raw_by = {r["raw_claim_id"]: r for r in raw}
    if len(raw_by) != len(raw):
        raise ValueError("Duplicate raw claim identity")
    links = set()
    missing = []
    raw_themes = defaultdict(set)
    theme_ids = set()
    for theme in themes:
        tid = theme["consensus_claim_id"]
        if not tid or tid in theme_ids:
            raise ValueError("Missing or duplicate final theme identity")
        theme_ids.add(tid)
        members = ids(theme.get("source_raw_claim_ids"))
        if not members:
            raise ValueError("Final theme has no raw member provenance: " + tid)
        for rid in sorted(members):
            if rid not in raw_by:
                raise ValueError("Final theme references missing raw claim: " + rid)
            raw_themes[rid].add(tid)
            source = raw_by[rid]["doc_id"]
            if source not in alias:
                raise ValueError(
                    "Final claim source missing from unified catalog: " + source
                )
            links.add((alias[source], tid, rid, "source_document", source, ""))
        for field in ("origin_doc_ids", "supporting_doc_ids"):
            for source in ids(theme.get(field)):
                if source not in alias:
                    raise ValueError(
                        "Final theme evidence document is unknown: " + source
                    )
                links.add((alias[source], tid, "", field, source, ""))
    for ref in references:
        members = ids(ref.get("source_raw_claim_ids")) & raw_themes.keys()
        if not members:
            continue
        doi = doi_key(ref.get("reference_doi"))
        doc = by_doi.get(doi)
        if available_reference_dois is not None and doi not in available_reference_dois:
            doc = None
        for rid in sorted(members):
            for tid in sorted(raw_themes[rid]):
                if doc and has_text(groups[doc]["chosen"]):
                    links.add(
                        (doc, tid, rid, "cited_reference", "", ref["reference_id"])
                    )
                else:
                    missing.append(
                        {
                            "theme_id": tid,
                            "raw_claim_id": rid,
                            "reference_id": ref["reference_id"],
                            "doi": doi,
                            "reason": "fulltext_unavailable_or_identity_conflict"
                            if doi
                            else "doi_unresolved",
                        }
                    )
    fields = [
        "doc_id",
        "theme_id",
        "raw_claim_id",
        "relation",
        "source_doc_id",
        "reference_id",
    ]
    rows = [dict(zip(fields, x)) for x in sorted(links)]
    selected = []
    for doc in sorted({r["doc_id"] for r in rows}):
        group = groups[doc]
        chosen = group["chosen"]
        if not has_text(chosen):
            raise ValueError("Selected source evidence has no extractable text: " + doc)
        own = [r for r in rows if r["doc_id"] == doc]
        selected.append(
            {
                "doc_id": doc,
                "doi": group["doi"],
                "identity_key": group["identity_key"],
                "source_id": chosen["source_id"],
                "source_path": chosen.get("input_prefix", "inputs/02_documents")
                + "/"
                + chosen["relative_path"],
                "source_variants": ";".join(
                    (r["source_id"] for r in group["variants"])
                ),
                "theme_ids": ";".join(sorted({r["theme_id"] for r in own})),
                "raw_claim_ids": ";".join(
                    sorted({r["raw_claim_id"] for r in own if r["raw_claim_id"]})
                ),
                "relations": ";".join(sorted({r["relation"] for r in own})),
            }
        )
    return (selected, rows, missing, groups)


def build_library(ctx, references):
    from runtime.online_references import acquire_references, POLICY as online_policy

    online_sources, acquired = acquire_references(ctx, references)
    sources = index_sources(ctx)
    sources.extend(online_sources)
    selected, links, missing, groups = select_documents(
        ctx.themes, ctx.raw, references, sources, set(acquired)
    )
    table(ctx.pending / "source_catalog.csv", sources)
    table(
        ctx.pending / "selected_documents.csv",
        selected,
        [
            "doc_id",
            "doi",
            "identity_key",
            "source_id",
            "source_path",
            "source_variants",
            "theme_ids",
            "raw_claim_ids",
            "relations",
        ],
    )
    table(
        ctx.pending / "claim_document_links.csv",
        links,
        [
            "doc_id",
            "theme_id",
            "raw_claim_id",
            "relation",
            "source_doc_id",
            "reference_id",
        ],
    )
    table(
        ctx.pending / "unavailable_references.csv",
        missing,
        ["theme_id", "raw_claim_id", "reference_id", "doi", "reason"],
    )
    chosen_ids = {r["doc_id"] for r in selected}
    table(
        ctx.pending / "collection_decisions.csv",
        [
            {
                "doc_id": doc,
                "source_ids": ";".join((r["source_id"] for r in g["variants"])),
                "selected": str(doc in chosen_ids).lower(),
                "reason": "linked_to_final_theme"
                if doc in chosen_ids
                else "no_resolved_final_theme_link",
            }
            for doc, g in sorted(groups.items())
        ],
        ["doc_id", "source_ids", "selected", "reason"],
    )
    reference_views = []
    for ref in references:
        ctx.controller.check()
        doi = doi_key(ref.get("reference_doi"))
        chosen = acquired.get(doi)
        if not chosen:
            continue
        relative = chosen["output"]
        reference_views.append(
            {
                "reference_id": ref["reference_id"],
                "doi": doi,
                "source_id": chosen["source_id"],
                "output": relative,
            }
        )
    table(
        ctx.pending / "reference_fulltext_links.csv",
        reference_views,
        ["reference_id", "doi", "source_id", "output"],
    )
    acquisition_rows = read_csv(ctx.pending / 'reference_acquisition.csv')
    ctx.emit(
        "library_summary.json",
        {
            "policy": online_policy,
            "reference_text_source": "current_run_online_downloads",
            "reference_status_counts": {
                status: sum(r['status'] == status for r in acquisition_rows)
                for status in ('available', 'unavailable', 'retrieval_failed')
            },
            "supporting_information": False,
            "final_themes": len(ctx.themes),
            "selected_documents": len(selected),
            "unavailable_reference_links": len(missing),
            "source_records": len(sources),
            "distinct_document_identities": len(groups),
            "identity_conflicts": sum((bool(r["doi_conflict"]) for r in sources)),
            "target_document_count": None,
            "extra_topic_search_executed": False,
            "model_calls": 0,
        },
    )


def validate_library(ctx):
    """Recompute membership from final themes before model work and at export."""
    folder = ctx.upstream(9, "")
    references = read_csv(folder / "reference_inventory.csv")
    sources = read_csv(folder / "source_catalog.csv")
    policy = read(folder / "library_summary.json").get("policy", POLICY)
    available = None
    from runtime.online_references import POLICY as online_policy

    if policy == online_policy:
        acquisition = read_csv(folder / "reference_acquisition.csv")
        available = {r["doi"] for r in acquisition if r["status"] == "available"}
        for row in acquisition:
            if row["status"] != "available":
                continue
            path = (folder / row["output"]).resolve()
            if (
                not path.is_relative_to((folder / "reference_documents").resolve())
                or not path.is_file()
            ):
                raise ValueError("Missing online reference article")
        expected_views = []
        by_doi = {r["doi"]: r for r in acquisition if r["status"] == "available"}
        for ref in references:
            doi = doi_key(ref.get("reference_doi"))
            if doi in by_doi:
                row = by_doi[doi]
                expected_views.append(
                    {
                        "reference_id": ref["reference_id"],
                        "doi": doi,
                        "source_id": row["source_id"],
                        "output": row["output"],
                    }
                )
        if read_csv(folder / "reference_fulltext_links.csv") != expected_views:
            raise ValueError(
                "Reference evidence must come from current-run online acquisitions"
            )
    selected, links, missing, _ = select_documents(
        ctx.themes, ctx.raw, references, sources, available
    )
    if read_csv(folder / "selected_documents.csv") != selected:
        raise ValueError("Reference library differs from final-claim membership")
    if read_csv(folder / "claim_document_links.csv") != links:
        raise ValueError("Claim-document links differ")
    if read_csv(folder / "unavailable_references.csv") != missing:
        raise ValueError("Unavailable reference links differ")
    from runtime.agent_contract import agent_state, stage_contract

    if getattr(ctx, "run", None) and agent_state(ctx.run) is not None:
        stage_contract(ctx.run, ctx.number)
    return {
        "policy": policy,
        "selected_documents": len(selected),
        "claim_document_links": len(links),
        "unavailable_reference_links": len(missing),
    }
