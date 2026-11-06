"""Current V3 recipes: exact scores and ID ties throughout the public pipeline."""

import copy
import json
import os
from functools import lru_cache
from pathlib import Path
from shared.runtime.paths import project_root

LOCKED_RANKING = {
    "formula_id": "v3_theme_pi_product",
    "evidence_max": 3,
    "impact_max": 4,
    "impact_top_fraction_denominator": 10,
    "impact_min_top_k": 3,
    "tie_break": "consensus_claim_id_asc",
    "display_decimals": 4,
    "df_unit": "unique_doc_id",
}
LOCKED_OVERRIDE = {
    "requires_actor": True,
    "requires_reason": True,
    "positive_corpus_support_required": True,
    "preserve_event_history": True,
}
SCOPES = [
    "tools.support.entity_frequency_from_outputs",
    "ranking.evidence_ranking",
    "platforms.selection_record",
]


def validate_ranking(ranking):
    if set(ranking) != set(LOCKED_RANKING):
        raise ValueError("Unsupported ranking fields")
    if ranking["formula_id"] not in {
        "configured_theme_pi_product",
        "df_only_v1",
        "df_gap_v1",
        "df_impact_v1",
        "v3_theme_pi_product",
    }:
        raise ValueError("Unknown implemented PI formula")
    for k in (
        "evidence_max",
        "impact_max",
        "impact_top_fraction_denominator",
        "impact_min_top_k",
    ):
        if type(ranking[k]) is not int or not 1 <= ranking[k] <= 100:
            raise ValueError(
                "Ranking scales and top-k parameters must be integers in 1..100"
            )
    if (
        type(ranking["display_decimals"]) is not int
        or not 0 <= ranking["display_decimals"] <= 12
    ):
        raise ValueError("Invalid ranking precision")
    expected_tie = "consensus_claim_id_asc"
    if ranking["tie_break"] != expected_tie or ranking["df_unit"] != "unique_doc_id":
        raise ValueError(
            "Unique-document df and deterministic tie breaking are mandatory"
        )
    if ranking["formula_id"] == "v3_theme_pi_product" and any(
        (
            ranking[k] != LOCKED_RANKING[k]
            for k in (
                "evidence_max",
                "impact_max",
                "impact_top_fraction_denominator",
                "impact_min_top_k",
            )
        )
    ):
        raise ValueError(
            "V3 formula requires its declared 0..3 E, 0..4 I and top-decile recipe"
        )


def validate_profile(value):
    version = value.get("schema")
    allowed = {
        "schema",
        "profile_id",
        "entity_specs",
        "ranking",
        "manual_override",
        "scope",
        "scoring_rubrics",
    }
    if set(value) != allowed or version != "entity.method_profile.v2":
        raise ValueError("Unsupported method profile schema or fields")
    if not isinstance(value["profile_id"], str) or not value["profile_id"].strip():
        raise ValueError("Explicit method profile ID required")
    validate_ranking(value["ranking"])
    if version == "entity.method_profile.v2":
        rubrics = value["scoring_rubrics"]
        if (
            not isinstance(rubrics, dict)
            or set(rubrics) != {"evidence", "impact", "impact_guardrails"}
            or any((not isinstance(v, str) or not v.strip() for v in rubrics.values()))
        ):
            raise ValueError(
                "v2 requires explicit evidence/impact rubrics and guardrails"
            )
    if value["manual_override"] != LOCKED_OVERRIDE or any(
        (type(x) is not bool for x in value["manual_override"].values())
    ):
        raise ValueError("Manual override safeguards are mandatory")
    if value["scope"] != SCOPES:
        raise ValueError("Method profile scope does not match implemented consumers")
    rows = value["entity_specs"]
    if not isinstance(rows, list):
        raise ValueError("Nonempty entity specifications required")
    names = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "name",
            "label",
            "category",
            "aliases",
        }:
            raise ValueError("Invalid entity specification")
        if (
            any(
                (
                    not isinstance(row[k], str) or not row[k].strip()
                    for k in ("name", "label", "category")
                )
            )
            or row["name"] in names
        ):
            raise ValueError("Duplicate or empty entity identity")
        names.add(row["name"])
        if (
            not isinstance(row["aliases"], list)
            or not row["aliases"]
            or any((not isinstance(x, str) or not x.strip() for x in row["aliases"]))
            or (len(row["aliases"]) != len(set(row["aliases"])))
        ):
            raise ValueError("Empty or duplicate entity alias")
    return copy.deepcopy(value)


def load_profile(path=None):
    path = Path(
        path
        or os.getenv("ENTITY_METHOD_CONFIG")
        or project_root().parent / "config/rules.json"
    )
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    return validate_profile(value.get("method", value))


_PROFILE = load_profile()


@lru_cache(maxsize=16)
def _active_profile(path):
    return load_profile(path)


def method_profile():
    path = str(
        Path(
            os.getenv("ENTITY_METHOD_CONFIG") or project_root().parent / "config/rules.json"
        ).resolve()
    )
    return copy.deepcopy(_active_profile(path))


def rubric(kind, default):
    return method_profile().get("scoring_rubrics", {}).get(kind, default)


def method_identity():
    snapshot = method_profile()
    return {
        "schema": snapshot["schema"],
        "profile_id": snapshot["profile_id"],
        "snapshot": snapshot,
    }
