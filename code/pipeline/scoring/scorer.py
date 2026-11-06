"""Raw-claim score formatting and source indexes used by execute_workflow."""
from __future__ import annotations
import csv, io, json
from pathlib import Path
from scoring.model_response import _to_int


OUTPUT_FIELDS = [
    "raw_claim_id",
    "raw_claim_text",
    "doc_id",
    "chunk_id",
    "page_number",
    "evidence_score_primary",
    "evidence_score_reference",
    "evidence_score_final",
    "evidence_final_source",
    "impact_score_llm",
    "evidence_primary_reasoning",
    "evidence_reference_reasoning",
    "impact_reasoning",
    "evidence_primary_checks_json",
    "evidence_reference_checks_json",
    "impact_checks_json",
    "cited_reference_dois",
    "primary_elapsed_s",
    "reference_elapsed_s",
    "impact_elapsed_s",
    "primary_prompt_chars",
    "reference_prompt_chars",
    "impact_prompt_chars",
    "version_tag",
]


def _index_abstracts(path: Path) -> dict[str, str]:
    """Load author-written abstracts. No fulltext/head fallback is allowed."""
    rows = _read_csv_skipping_comments(path)
    return {
        str(row.get("doc_id", "")).strip(): str(row.get("abstract_text", "")).strip()
        for row in rows
        if str(row.get("doc_id", "")).strip()
        and str(row.get("abstract_text", "")).strip()
    }


def _index_structured_summaries(path: Path) -> dict[str, dict[str, str]]:
    rows = _read_csv_skipping_comments(path)
    return {
        str(row.get("doc_id", "")).strip(): row
        for row in rows
        if str(row.get("doc_id", "")).strip()
    }


def _index_dois_by_raw_claim(inventory_rows,pool_claims):
    import unicodedata
    def normalized(text):return ' '.join(unicodedata.normalize('NFC',str(text)).split())
    options={}
    for row in inventory_rows:
        text=normalized(row.get('reference_text',''))
        doi=str(row.get('reference_doi') or row.get('doi') or '').strip()
        if text and doi:options.setdefault(text,set()).add(doi)
    result={}
    for claim in pool_claims:
        entries=json.loads(claim.get('reference_used','[]') or '[]')
        resolved=[]
        for entry in entries:
            matches=options.get(normalized(entry.get('reference_text','')),set())
            if len(matches)==1:
                doi=next(iter(matches))
                if doi not in resolved:resolved.append(doi)
        if resolved:result[claim['raw_claim_id']]=resolved
    return result


def _read_csv_skipping_comments(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig") as f:
        lines = [ln for ln in f if not ln.lstrip().startswith("#")]
    return list(csv.DictReader(io.StringIO("".join(lines))))


def _build_output_row(
    *,
    claim: dict[str, str],
    primary: dict[str, object],
    reference: dict[str, object] | None,
    impact: dict[str, object],
    final_score: str,
    final_source: str,
    ref_dois: list[str],
) -> dict[str, str]:
    return {
        "raw_claim_id": claim["raw_claim_id"],
        "raw_claim_text": claim.get("raw_claim_text", ""),
        "doc_id": claim.get("doc_id", ""),
        "chunk_id": claim.get("chunk_id", ""),
        "page_number": claim.get("page_number", ""),
        "evidence_score_primary": _format_optional_score(primary.get("evidence_score")),
        "evidence_score_reference": (
            _format_optional_score(reference.get("evidence_score")) if reference else ""
        ),
        "evidence_score_final": final_score,
        "evidence_final_source": final_source,
        "impact_score_llm": _format_optional_score(impact.get("impact_score")),
        "evidence_primary_reasoning": str(primary.get("reasoning", "")),
        "evidence_reference_reasoning": str(
            reference.get("reasoning", "") if reference else ""
        ),
        "impact_reasoning": str(impact.get("reasoning", "")),
        "evidence_primary_checks_json": json.dumps(
            primary.get("evidence_checks", {}), ensure_ascii=False
        ),
        "evidence_reference_checks_json": json.dumps(
            reference.get("evidence_checks", {}) if reference else {},
            ensure_ascii=False,
        ),
        "impact_checks_json": json.dumps(
            impact.get("impact_checks", {}), ensure_ascii=False
        ),
        "cited_reference_dois": json.dumps(ref_dois, ensure_ascii=False),
        "primary_elapsed_s": f"{primary.get('elapsed', 0):.1f}",
        "reference_elapsed_s": (
            f"{reference.get('elapsed', 0):.1f}" if reference else ""
        ),
        "impact_elapsed_s": f"{impact.get('elapsed', 0):.1f}",
        "primary_prompt_chars": str(primary.get("prompt_chars", 0)),
        "reference_prompt_chars": (
            str(reference.get("prompt_chars", 0)) if reference else ""
        ),
        "impact_prompt_chars": str(impact.get("prompt_chars", 0)),
        "version_tag": "raw_claim_v1",
    }


def _format_optional_score(value: object) -> str:
    score = _to_int(value)
    return "" if score is None else str(score)
