"""Count existing model keywords directly; no quote/DOI/alias-match acceptance gate."""

from collections import Counter, defaultdict
from pathlib import Path
from platforms import support as job


def main(out, dest):
    manifest = job.read(out / "manifest.json")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    rows = []
    covered = Counter()
    missing = []
    cache_sources = []
    for task in manifest["tasks"]:
        cache = out / task["response_file"]
        if not cache.exists():
            missing.append(task["task_id"])
            continue
        value = job.read(cache)
        covered[task["doc_id"]] += 1
        cache_sources.append(
            {"task_id": task["task_id"], "path": task["response_file"]}
        )
        for index, e in enumerate(value.get("entities", [])):
            rows.append(
                {
                    "doc_id": task["doc_id"],
                    "task_id": task["task_id"],
                    "index": index,
                    "name": e.get("canonical_name")
                    or e.get("surface_name")
                    or "unknown",
                    "surface_name": e.get("surface_name", ""),
                    "aliases": e.get("aliases", []),
                    "role": e.get("role", "uncertain"),
                    "centers": e.get("active_center_elements", []),
                    "structure": e.get("structure_type", ""),
                    "mention_context": e.get("mention_context", "uncertain"),
                    "entity_relation": e.get("entity_relation", "uncertain"),
                    "reaction_scope": e.get("reaction_scope", "uncertain"),
                }
            )
    centers = defaultdict(set)
    own = defaultdict(set)
    entity = defaultdict(set)
    keyword_docs = defaultdict(set)
    dictionaries = {}
    photos = []
    uncertain = []
    known_center_names = set()
    unknown_centers = []
    for row in rows:
        ident = (
            job.lexical(row["name"]),
            row["role"],
            tuple(sorted(set(row["centers"]))),
        )
        keyword_docs[ident].add(row["doc_id"])
        d = dictionaries.setdefault(
            ident,
            {
                "name": row["name"],
                "role": row["role"],
                "centers": list(ident[2]),
                "aliases_by_document": defaultdict(set),
                "structure_labels": set(),
                "documents": set(),
            },
        )
        d["aliases_by_document"][row["doc_id"]].update(
            [row["surface_name"], *row["aliases"]]
        )
        d["structure_labels"].add(row["structure"])
        d["documents"].add(row["doc_id"])
        if row["role"] in ("photosensitizer", "both"):
            photos.append(row)
        if row["role"] == "uncertain":
            uncertain.append(row)
        if row["role"] in ("catalyst", "both"):
            if not row["centers"]:
                unknown_centers.append(row)
            for center in set(row["centers"]):
                centers[center].add(row["doc_id"])
                known_center_names.add(center)
                if row["mention_context"] == "authors_experiment":
                    own[center].add(row["doc_id"])
                if row["entity_relation"] == "explicit":
                    entity[center].add(row["doc_id"])
    rankings = sorted(
        [
            {
                "element": c,
                "documents": len(ds),
                "authors_experiment_documents": len(own[c]),
                "kind": "metal"
                if c in job.ELEMENTS - job.NONMETALS
                else "nonmetal_or_metalloid"
                if c in job.NONMETALS
                else "model_other_label",
            }
            for c, ds in centers.items()
        ],
        key=lambda r: (-r["documents"], r["element"]),
    )
    for kind in ("metal", "nonmetal_or_metalloid", "model_other_label"):
        n = 0
        for r in rankings:
            if r["kind"] == kind:
                n += 1
                r["rank"] = n
    fields = ["rank", "element", "documents", "authors_experiment_documents", "kind"]
    job.csvwrite(
        dest / "metal_center_ranking.csv",
        [r for r in rankings if r["kind"] == "metal"],
        fields,
    )
    job.csvwrite(
        dest / "other_center_ranking.csv",
        [r for r in rankings if r["kind"] != "metal"],
        fields,
    )
    keywords = []
    clean = []
    for ident, d in dictionaries.items():
        keywords.append(
            {
                "name": d["name"],
                "role": d["role"],
                "active_centers": "; ".join(d["centers"]),
                "documents": len(d["documents"]),
                "aliases": "; ".join(
                    sorted({s for v in d["aliases_by_document"].values() for s in v})
                ),
                "structure_labels": "; ".join(sorted(d["structure_labels"])),
            }
        )
        clean.append(
            {
                **d,
                "structure_labels": sorted(d["structure_labels"]),
                "documents": sorted(d["documents"]),
                "aliases_by_document": {
                    doc: sorted(v) for doc, v in d["aliases_by_document"].items()
                },
            }
        )
    keywords.sort(key=lambda r: (-r["documents"], r["name"]))
    kfields = [
        "name",
        "role",
        "active_centers",
        "documents",
        "aliases",
        "structure_labels",
    ]
    job.csvwrite(dest / "all_keyword_counts.csv", keywords, kfields)
    job.csvwrite(
        dest / "photosensitizer_keyword_counts.csv",
        [r for r in keywords if r["role"] in ("photosensitizer", "both")],
        kfields,
    )
    job.csvwrite(
        dest / "uncertain_keyword_counts.csv",
        [r for r in keywords if r["role"] == "uncertain"],
        kfields,
    )
    job.save(dest / "keyword_dictionary.json", clean)
    job.save(dest / "semantic_observations.json", rows)
    expected = Counter((t["doc_id"] for t in manifest["tasks"]))
    doccoverage = [
        {
            "doc_id": d,
            "completed_parts": covered[d],
            "total_parts": n,
            "complete": covered[d] == n,
        }
        for d, n in expected.items()
    ]
    job.csvwrite(
        dest / "document_coverage.csv",
        doccoverage,
        ["doc_id", "completed_parts", "total_parts", "complete"],
    )
    scoped = defaultdict(set)
    for row in rows:
        if (
            row["role"] in ("catalyst", "both")
            and row["reaction_scope"] == "photocatalytic_co2_reduction"
        ):
            for center in row["centers"]:
                scoped[center].add(row["doc_id"])
    scope_rows = [
        {"rank": n + 1, "element": c, "documents": len(ds)}
        for n, (c, ds) in enumerate(
            sorted(scoped.items(), key=lambda pair: (-len(pair[1]), pair[0]))
        )
    ]
    job.csvwrite(
        dest / "photocatalytic_co2_center_ranking.csv",
        scope_rows,
        ["rank", "element", "documents"],
    )
    summary = {
        "state": "completed" if not missing else "partial",
        "model": job.MODEL,
        "documents": len(expected),
        "fully_processed_documents": sum((r["complete"] for r in doccoverage)),
        "total_tasks": len(manifest["tasks"]),
        "completed_tasks": len(cache_sources),
        "missing_tasks": missing,
        "semantic_observations": len(rows),
        "keyword_groups": len(keywords),
        "role_counts": dict(Counter((r["role"] for r in rows))),
        "unknown_center_observations": len(unknown_centers),
        "metal_ranking": [r for r in rankings if r["kind"] == "metal"],
        "doi_lookup": False,
        "extra_model_review": False,
        "quote_or_alias_gate": False,
        "main_count_scope": "Authors experiments plus literature discussions, as labeled by mini.",
        "semantic_roles_not_independently_verified": True,
    }
    return summary
