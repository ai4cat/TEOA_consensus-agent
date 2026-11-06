"""Retrieve name-hit chunks using only the upstream selected entity and aliases."""

import unicodedata
from preparation.entity_frequency import _compile_alias_matcher


def select_target_chunks(chunk_rows, doc_rows, entity):
    aliases = tuple(dict.fromkeys((entity.name, *entity.aliases)))
    matchers = {
        alias: _compile_alias_matcher((unicodedata.normalize("NFKC", alias),))
        for alias in aliases
    }
    hits = {}
    eligible = []
    selected = []
    chunk_hits = []
    for row in chunk_rows:
        text = str(row.get("text") or row.get("clean_text") or "").strip()
        if row.get("chunk_type") == "reference_entry" or not text:
            continue
        eligible.append(row)
        normalized = unicodedata.normalize("NFKC", text)
        matched = [
            alias for alias, pattern in matchers.items() if pattern.search(normalized)
        ]
        if matched:
            selected.append(row)
            chunk_hits.append(
                {
                    "doc_id": str(row["doc_id"]),
                    "chunk_id": str(row["chunk_id"]),
                    "matched_aliases": matched,
                }
            )
            doc_id = str(row["doc_id"])
            hit = hits.setdefault(doc_id, {"aliases": set(), "chunk_ids": []})
            hit["aliases"].update(matched)
            hit["chunk_ids"].append(str(row["chunk_id"]))
    docs = [r for r in doc_rows if str(r.get("doc_id", "")) in hits]
    report = {
        "policy": "upstream-selected-entity-name-hit-chunks-only",
        "entity": entity.record(),
        "input_document_count": len({str(r["doc_id"]) for r in chunk_rows}),
        "selected_document_count": len(hits),
        "input_nonbibliographic_chunks": len(eligible),
        "selected_nonbibliographic_chunks": len(selected),
        "matching_chunk_count": sum(len(v["chunk_ids"]) for v in hits.values()),
        "documents": [
            {
                "doc_id": doc_id,
                "matched_aliases": sorted(v["aliases"]),
                "matching_chunk_ids": v["chunk_ids"],
            }
            for doc_id, v in sorted(hits.items())
        ],
        "chunks": chunk_hits,
        "limitations": [
            "Name occurrence is a retrieval criterion, not proof of additive use or role.",
            "Chunks using only pronouns, missing aliases, or OCR-damaged names are not retrieved.",
        ],
    }
    return selected, docs, report
