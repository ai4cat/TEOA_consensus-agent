"""Exact rational reconstruction using the documented V3 theme priority formula."""

from fractions import Fraction
from itertools import combinations

FORMULA_ID = "v3_theme_pi_product"


def index(rows, key):
    result = {}
    for row in rows:
        value = str(row.get(key, "")).strip()
        if not value or value in result:
            raise ValueError(f"Missing or duplicate {key}: {value}")
        result[value] = row
    return result


def score(value, maximum, field):
    if isinstance(value, bool):
        raise ValueError(f"Invalid {field}: Boolean")
    try:
        number = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        raise ValueError(f"Invalid {field}") from None
    if number.denominator != 1 or not 0 <= number <= maximum:
        raise ValueError(f"Invalid {field} range")
    return number


def reconstruct_v3(themes, claims, scores):
    """All raw members define df; only explicitly scored members define E and I.

    Missing raw claims, duplicate members and contradictory df fail closed.
    Unscored members remain counted and never receive invented zero scores.
    """
    raw = index(claims, "raw_claim_id")
    if not themes or not claims or not scores:
        raise ValueError("Nonempty themes, claims and scores required")
    scored = index(scores, "raw_claim_id")
    index(themes, "consensus_claim_id")
    for identifier, row in scored.items():
        if identifier not in raw:
            raise ValueError(f"Score has no raw claim: {identifier}")
        if row.get("doc_id") and row["doc_id"] != raw[identifier].get("doc_id"):
            raise ValueError(f"Score document mismatch: {identifier}")
        score(row.get("evidence_score_final"), 3, "evidence_score_final")
        score(row.get("impact_score_llm"), 4, "impact_score_llm")
    result = []
    for theme in themes:
        identifier = theme["consensus_claim_id"]
        members = [
            x.strip()
            for x in theme.get("source_raw_claim_ids", "").split(";")
            if x.strip()
        ]
        if not members or len(members) != len(set(members)):
            raise ValueError(f"Empty or duplicate members: {identifier}")
        missing = set(members) - set(raw)
        if missing:
            raise ValueError(f"Missing raw members for {identifier}: {sorted(missing)}")
        docs = {raw[x].get("doc_id", "") for x in members}
        if "" in docs:
            raise ValueError(f"Member document missing: {identifier}")
        df = len(docs)
        if Fraction(str(theme.get("df", ""))) != df:
            raise ValueError(f"Theme df mismatch: {identifier}")
        active = [scored[x] for x in members if x in scored]
        if not active:
            raise ValueError(f"No scored members: {identifier}")
        n = len(active)
        k = min(n, max(3, (n + 9) // 10))
        e = sum(score(x["evidence_score_final"], 3, "E") for x in active) / n
        impacts = sorted(
            (score(x["impact_score_llm"], 4, "I") for x in active), reverse=True
        )
        i = sum(impacts[:k]) / k
        gap = Fraction(df) * (1 - e / 3)
        p = gap * i / 4
        result.append(
            {
                "consensus_claim_id": identifier,
                "consensus_claim_text": theme.get("consensus_claim_text", ""),
                "formula_id": FORMULA_ID,
                "df": df,
                "raw_member_count": len(members),
                "scored_member_count": n,
                "unscored_member_count": len(members) - n,
                "impact_top_k": k,
                "mean_evidence_exact": str(e),
                "top_impact_exact": str(i),
                "full_PI_exact": str(p),
                "df_gap_exact": str(gap),
                "mean_evidence": float(e),
                "top_impact_mean": float(i),
                "PI_unrounded": float(p),
                "PI_four_decimals": f"{float(p):.4f}",
                "run_mode": "online_run",
                "scoring_mode": "model_execution",
                "generation_mode": "fresh_model_responses_with_local_aggregation",
                "tie_break": "unrounded_exact_score_desc_then_consensus_claim_id_asc",
            }
        )
    result.sort(key=lambda r: (-Fraction(r["full_PI_exact"]), r["consensus_claim_id"]))
    return [{"rank": n, **row} for n, row in enumerate(result, 1)]


def sensitivity(reconstructed, topks=(3, 5)):
    methods = {
        "df_only": lambda r: Fraction(r["df"]),
        "df_gap": lambda r: Fraction(r["df_gap_exact"]),
        "full_PI": lambda r: Fraction(r["full_PI_exact"]),
    }
    orders = {
        name: sorted(reconstructed, key=lambda r: (-f(r), r["consensus_claim_id"]))
        for name, f in methods.items()
    }
    ranks = {
        name: {r["consensus_claim_id"]: n for n, r in enumerate(rows, 1)}
        for name, rows in orders.items()
    }
    rows = [
        {
            "consensus_claim_id": r["consensus_claim_id"],
            "formula_id": FORMULA_ID,
            "run_mode": "online_run",
            "scoring_mode": "model_execution",
            "generation_mode": "fresh_model_responses_with_local_aggregation",
            **{name: ranks[name][r["consensus_claim_id"]] for name in methods},
        }
        for r in reconstructed
    ]
    overlaps = []
    for a, b in combinations(methods, 2):
        for k in topks:
            if type(k) is not int or k < 1:
                raise ValueError("Positive integer top-k required")
            aa = {r["consensus_claim_id"] for r in orders[a][:k]}
            bb = {r["consensus_claim_id"] for r in orders[b][:k]}
            overlaps.append(
                {
                    "method_a": a,
                    "method_b": b,
                    "requested_k": k,
                    "effective_k": min(k, len(reconstructed)),
                    "intersection": len(aa & bb),
                    "union": len(aa | bb),
                    "jaccard": len(aa & bb) / len(aa | bb),
                    "only_a": ";".join(sorted(aa - bb)),
                    "only_b": ";".join(sorted(bb - aa)),
                    "formula_id": FORMULA_ID,
                    "run_mode": "online_run",
                    "scoring_mode": "model_execution",
                    "generation_mode": "fresh_model_responses_with_local_aggregation",
                }
            )
    return rows, overlaps
