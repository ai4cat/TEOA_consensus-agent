"""LLM vocabulary nomination followed by deterministic, unweighted corpus counting."""

import json

import jsonschema

from preparation.entity_frequency import EntitySpec, build_entity_frequency_rows

PROMPT = "In photocatalytic CO2 reduction, keeping the initial catalyst type, composition, and loading unchanged, list 10 common additives added directly to the reaction mixture to adjust activity or selectivity. Provide each additive's English name and common abbreviations only. Use English throughout. Exclude solvent-environment and medium effects. Return JSON matching the supplied schema."
SCHEMA = {
    "type": "object",
    "properties": {
        "terms": {
            "type": "array",
            "minItems": 10,
            "maxItems": 10,
            "items": {
                "type": "object",
                "properties": {
                    "english_name": {"type": "string"},
                    "abbreviations": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["english_name", "abbreviations"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["terms"],
    "additionalProperties": False,
}


def vocabulary_specs(value):
    """Validate structure and collisions; do not claim chemical synonym certification."""
    jsonschema.validate(value, SCHEMA)
    names, owners, specs = set(), {}, []
    for row in value["terms"]:
        name = row["english_name"].strip()
        if not name or name.casefold() in names:
            raise ValueError("Ten distinct English-named substances are required")
        names.add(name.casefold())
        aliases = tuple(dict.fromkeys(a.strip() for a in [name, *row["abbreviations"]] if a.strip()))
        if any("\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff"
               for alias in aliases for char in alias):
            raise ValueError("Candidate names and abbreviations must use English")
        for alias in aliases:
            owner = owners.setdefault(alias.casefold(), name)
            if owner != name:
                raise ValueError("An alias belongs to multiple candidates: " + alias)
        specs.append(EntitySpec(name, name, "candidate_additive", aliases))
    return tuple(specs)


def expected_content():
    return (
        PROMPT
        + "\nINPUT_JSON\n{}\nOUTPUT_JSON_SCHEMA\n"
        + json.dumps(SCHEMA, ensure_ascii=False)
    )


def rank_candidates(chunks, value):
    specs = vocabulary_specs(value)
    body = [r for r in chunks if r.get("chunk_type") == "body_paragraph"]
    if not body or any(not r.get("doc_id") or not r.get("chunk_id") for r in body):
        raise ValueError("Body paragraphs require document and chunk identities")
    ids = [(r["doc_id"], r["chunk_id"]) for r in body]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate body paragraph identity")
    rows = build_entity_frequency_rows(body, [], entity_specs=specs)
    by_name = {r["entity_name"]: r for r in rows}
    # Include zero-hit candidates so the exported table always represents all ten.
    from preparation.entity_frequency import ENTITY_FREQUENCY_FIELDS

    all_rows = []
    for spec in specs:
        row = by_name.get(spec.name)
        if row is None:
            row = {k: 0 for k in ENTITY_FREQUENCY_FIELDS}
            row.update(
                entity_name=spec.name,
                entity_label=spec.label,
                entity_category="candidate_additive",
                aliases="; ".join(spec.aliases),
                top_doc_ids="",
                top_chunk_ids="",
            )
        all_rows.append(row)
    all_rows.sort(
        key=lambda r: (
            -int(r["chunk_mentions"]),
            -int(r["chunk_doc_count"]),
            r["entity_name"],
        )
    )
    for rank, row in enumerate(all_rows, 1):
        row["rank"] = rank
    top = int(all_rows[0]["chunk_mentions"])
    tied = [r["entity_name"] for r in all_rows if int(r["chunk_mentions"]) == top]
    winner = all_rows[0]["entity_name"] if top > 0 and len(tied) == 1 else None
    record = {
        "schema": "entity.entity_nomination.v2",
        "method": "llm_candidates_merged_name_frequency",
        "selected_entity": winner,
        "tied_entities": tied if top > 0 and len(tied) > 1 else [],
        "status": (
            "nominated"
            if winner
            else "tie_requires_researcher_selection" if top > 0 else "no_hits"
        ),
        "ranking_rule": "descending merged chunk_mentions; no role filter or weights; ties are not auto-selected",
        "body_paragraphs": len(body),
        "document_count": len({r["doc_id"] for r in body}),
        "raw_claims_used": False,
        "candidate_count": len(specs),
        "actual_additive_use_verified": False,
        "selected_aliases": (
            list(next(s.aliases for s in specs if s.name == winner)) if winner else []
        ),
        "top_mentions": top,
        "limitations": [
            "Full-corpus mentions include products, solvents and background.",
            "LLM synonyms and short abbreviations are not chemically certified.",
        ],
    }
    return all_rows, record


