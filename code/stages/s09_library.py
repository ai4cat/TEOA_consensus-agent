"""Stage 9: resolve citations, fetch online main articles, and extract local text."""

import json, unicodedata
from scoring.references import reference_enrichment as resolver
from shared.pdf_utils import extract_doi
from runtime.artifacts import save, table
from runtime.reference_http import ReferenceHTTP


def run(ctx):
    grouped = {}
    for claim in ctx.scoring_raw:
        entries = json.loads(claim.get("reference_used", "[]") or "[]")
        for entry in entries:
            text = " ".join(
                unicodedata.normalize(
                    "NFC", str(entry.get("reference_text", ""))
                ).split()
            )
            if not text:
                continue
            row = grouped.setdefault(text, {"claims": set(), "docs": set()})
            row["claims"].add(claim["raw_claim_id"])
            row["docs"].add(claim["doc_id"])
    http = ReferenceHTTP(ctx.communications / "http", ctx.controller)
    original = resolver.requests
    resolver.requests = http
    checkpoints = ctx.work / "resolved"
    checkpoints.mkdir(exist_ok=True)
    rows = []
    try:
        for index, (text, links) in enumerate(sorted(grouped.items()), 1):
            ctx.controller.check()
            key = f"{index:08d}"
            path = checkpoints / (key + ".json")
            event_start = len(http.events)
            doi = extract_doi(text)
            if doi:
                value = {
                    "identifier_value": doi,
                    "identifier_source": "reference_text",
                    "lookup_status": "doi_in_reference",
                }
            else:
                before = http.successes
                value = resolver.resolve_reference_metadata(text)
                ctx.controller.check()
                if not value.get("identifier_value") and http.successes == before:
                    raise RuntimeError(
                        "No bibliographic provider returned usable JSON; pause/retry rather than finalize a missing reference"
                    )
            value["http_requests"] = http.events[event_start:]
            save(path, value)
            rows.append(
                {
                    "reference_id": "REF_" + key,
                    "reference_text": text,
                    "reference_doi": value.get("identifier_value", ""),
                    "reference_doi_source": value.get("identifier_source", ""),
                    "reference_lookup_status": value.get("lookup_status", ""),
                    "source_raw_claim_ids": ";".join(sorted(links["claims"])),
                    "source_doc_ids": ";".join(sorted(links["docs"])),
                    "source_claim_count": len(links["claims"]),
                    "lookup_requests_json": json.dumps(
                        value.get("http_requests", []), ensure_ascii=False
                    ),
                }
            )
            ctx.status(state="references", completed=len(rows), total=len(grouped))
    finally:
        resolver.requests = original
        http.close()
    fields = [
        "reference_id",
        "reference_text",
        "reference_doi",
        "reference_doi_source",
        "reference_lookup_status",
        "source_raw_claim_ids",
        "source_doc_ids",
        "source_claim_count",
        "lookup_requests_json",
    ]
    table(ctx.pending / "reference_inventory.csv", rows, fields)
    ctx.emit(
        "summary.json",
        {
            "references": len(rows),
            "resolved_dois": sum(bool(r["reference_doi"]) for r in rows),
            "historical_reference_lookup_table_used": False,
            "llm_calls": 0,
        },
    )

    from runtime.library import build_library

    build_library(ctx, rows)
