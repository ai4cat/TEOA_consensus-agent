from __future__ import annotations

from scoring.model_response import _call_llm
from shared.selected_entity import entity_instruction
from scoring.scoring.scoring_rubrics import (
    IMPACT_GUARDRAILS,
    IMPACT_SCORE_RUBRIC,
)

IMPACT_MAX_TOKENS = 1400


def _score_impact(
    *,
    claim_text: str,
    chunk_text: str,
    abstract_text: str,
    structured_summary: dict[str, str],
    llm_env: dict[str, str],
) -> dict[str, object]:
    # --- INPUT A: author-written abstract (ground truth for main conclusions) ---
    abstract_block = (
        abstract_text if abstract_text else "(original abstract not available)"
    )

    # --- INPUT B: structured evidence summary (pre-processed to match rubric dimensions) ---
    summary_fields = [
        ("mechanism_summary", "Mechanism conclusion"),
        ("design_rule_summary", "Catalyst design rule"),
        ("selectivity_summary", "Selectivity explanation"),
        ("controls_summary", "Key experiments / controls"),
        ("target_role_summary", "Source-stated target role"),
        ("contradictions_summary", "Contradictions / contested claims"),
        ("limitations_summary", "Limitations / generalization risks"),
    ]
    summary_lines = []
    for field, label in summary_fields:
        val = str(structured_summary.get(field, "")).strip()
        if val:
            summary_lines.append(f"  {label}: {val}")
    structured_block = (
        "\n".join(summary_lines)
        if summary_lines
        else "(structured summary not available)"
    )

    prompt = (
        entity_instruction()
        + "You are a chemistry claim auditor scoring IMPACT only.\n"
        "The target role refers to the selected entity above, not another component of the paper. "
        "If a supplied target-role summary describes another component, do not treat it as evidence of the selected entity's role. "
        "Assess the supplied claim from its source evidence without assuming the selected entity is central.\n"
        "Question: if this claim turned out to be FALSE, how much would the source paper's "
        "central conclusions shift?\n\n"
        "CRITICAL DISTINCTION :read before scoring:\n"
        "  TYPE A :EXPERIMENTAL CONDITIONS / SETUP: claims that only describe HOW the experiment\n"
        "    was done (reagents added, amounts used, system composition, reaction carried out in X).\n"
        "    These usually score 0-1, regardless of the paper's conclusions.\n"
        "    Examples: 'the selected additive was added as sacrificial agent', 'reaction was carried out in CH3CN/the selected additive',\n"
        "    '10 mg catalyst was suspended in the selected additive solution', 'an additive was used as electron donor'.\n\n"
        "  TYPE B :MECHANISM / RESULT / INTERPRETATION: claims that assert WHAT was found,\n"
        "    WHY it works, or WHAT role a component plays.\n"
        "    These can score 0-4 based on centrality to the paper's conclusions.\n"
        "    Examples: 'the selected additive slows decomposition of g-C3N4', 'the selected additive reductively quenches a photosensitizer',\n"
        "    'hole scavenging by the selected additive is the rate-limiting step'.\n\n"
        "First classify the claim as TYPE A or TYPE B, then score accordingly.\n\n"
        "To answer this, you are given two complementary inputs about the source paper. "
        "Weigh them together to form a complete picture before scoring:\n\n"
        "  INPUT A :PAPER ABSTRACT: the author's own statement of the paper's main contribution "
        "and findings. Tells you what the paper is fundamentally about.\n\n"
        "  INPUT B :STRUCTURED EVIDENCE SUMMARY: a breakdown of the paper's mechanism, "
        "design rules, controls, target role, and limitations :each field maps directly to "
        "a dimension of the impact rubric. Tells you how the claim connects to each dimension.\n\n"
        "Use both inputs together: INPUT A anchors the paper's main conclusions; "
        "INPUT B shows where the claim sits within those conclusions. "
        "A claim that appears central in INPUT B but is peripheral in INPUT A should score lower. "
        "A claim that is absent from INPUT A but critical to a mechanism in INPUT B may still score high "
        "if the mechanism is the paper's core contribution.\n\n"
        "Return JSON only with these keys:\n"
        "  claim_type              (string: 'experimental_condition' or 'mechanism_result')\n"
        "  impact_score            (integer 0-4)\n"
        "  reasoning               (under 30 words)\n"
        "  why_not_lower           (under 30 words)\n"
        "  why_not_higher          (under 30 words)\n"
        "  decision_relevance      (under 30 words)\n"
        "  impact_checks           (object with booleans: changes_mechanism, "
        "changes_design_rule, changes_selectivity_explanation, "
        "changes_key_experiment_or_control, changes_screening_route, "
        "only_local_discussion, only_wording_level)\n\n"
        "Do not use markdown fences. Do not omit any key.\n\n"
        "==== IMPACT RUBRIC ====\n\n"
        f"{IMPACT_SCORE_RUBRIC}\n"
        f"{IMPACT_GUARDRAILS}\n"
        "==== CLAIM ====\n\n"
        f"{claim_text}\n\n"
        "==== SOURCE PARAGRAPH (paragraph where the claim appears in the paper) ====\n\n"
        f"{chunk_text or '(source paragraph unavailable)'}\n\n"
        "==== INPUT A :PAPER ABSTRACT (author-written; what the paper is fundamentally about) ====\n\n"
        f"{abstract_block}\n\n"
        "==== INPUT B :STRUCTURED EVIDENCE SUMMARY (rubric-aligned; how the claim connects to each dimension) ====\n\n"
        f"{structured_block}\n"
    )
    return _call_llm(prompt, llm_env, max_tokens=IMPACT_MAX_TOKENS)
