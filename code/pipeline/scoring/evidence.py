from __future__ import annotations

from pathlib import Path

from scoring.model_response import _call_llm
from scoring.references.reference_content import load_reference_fulltext
from scoring.scoring.scoring_rubrics import (
    EVIDENCE_STRENGTH_RUBRIC,
)

EVIDENCE_MAX_TOKENS = 1400
REFERENCE_FULLTEXT_TRUNCATE = 18000


def _score_evidence_primary(
    *, claim_text: str, anchor_text: str, chunk_text: str, llm_env: dict[str, str]
) -> dict[str, object]:
    anchor_block = (
        f"==== ANCHOR SENTENCE (the exact sentence the claim is based on) ====\n\n"
        f"{anchor_text}\n\n"
        if anchor_text
        else ""
    )
    prompt = (
        "You are a chemistry claim auditor scoring EVIDENCE STRENGTH only. "
        "Score how well the SOURCE PARAGRAPH supports the claim, using the rubric below.\n\n"
        "The ANCHOR SENTENCE (if provided) is the exact sentence from which the claim was extracted. "
        "Focus your evidence assessment on that sentence and its immediate context in the paragraph.\n\n"
        "Return JSON only with these keys:\n"
        "  evidence_score          (integer 0-3)\n"
        "  reasoning               (under 30 words)\n"
        "  key_passage             (verbatim quote from the Source Paragraph)\n"
        "  evidence_checks         (object with booleans: direct_evidence_chain, "
        "partial_controls, indirect_inference, default_assumption_only)\n\n"
        "Do not use markdown fences. Do not omit any key.\n\n"
        "==== EVIDENCE STRENGTH RUBRIC ====\n\n"
        f"{EVIDENCE_STRENGTH_RUBRIC}\n"
        "==== CLAIM ====\n\n"
        f"{claim_text}\n\n"
        f"{anchor_block}"
        "==== SOURCE PARAGRAPH ====\n\n"
        f"{chunk_text or '(source paragraph unavailable)'}\n"
    )
    return _call_llm(prompt, llm_env, max_tokens=EVIDENCE_MAX_TOKENS)


def _score_evidence_reference(
    *, claim_text: str, ref_dois: list[str], reference_root: Path, llm_env: dict[str, str]
) -> dict[str, object] | None:
    if not ref_dois:
        return None
    blocks: list[str] = []
    cited_dois: list[str] = []
    for doi in ref_dois[:6]:
        text = load_reference_fulltext(reference_root, doi)
        if not text:
            continue
        if len(text) > REFERENCE_FULLTEXT_TRUNCATE:
            text = text[:REFERENCE_FULLTEXT_TRUNCATE] + "\n\n[... truncated ...]"
        blocks.append(f"--- REFERENCE DOI: {doi} ---\n{text}")
        cited_dois.append(doi)
    if not blocks:
        return None
    refs_concat = "\n\n".join(blocks)
    prompt = (
        "You are a chemistry claim auditor scoring EVIDENCE STRENGTH only. "
        "Score whether the cited REFERENCE PAPERS provide evidence for the claim, "
        "using the rubric below.\n\n"
        "Return JSON only with these keys:\n"
        "  evidence_score          (integer 0-3)\n"
        "  reasoning               (under 30 words)\n"
        "  key_passage             (verbatim quote from a Reference)\n"
        "  cited_reference_dois    (DOIs from the references you actually used)\n"
        "  evidence_checks         (object with booleans: direct_evidence_chain, "
        "partial_controls, indirect_inference, default_assumption_only)\n\n"
        "Do not use markdown fences. Do not omit any key.\n\n"
        "==== EVIDENCE STRENGTH RUBRIC ====\n\n"
        f"{EVIDENCE_STRENGTH_RUBRIC}\n"
        "==== CLAIM ====\n\n"
        f"{claim_text}\n\n"
        "==== REFERENCE FULLTEXTS ====\n\n"
        f"{refs_concat}\n"
    )
    result = _call_llm(prompt, llm_env, max_tokens=EVIDENCE_MAX_TOKENS)
    result["cited_dois_input"] = cited_dois
    return result
