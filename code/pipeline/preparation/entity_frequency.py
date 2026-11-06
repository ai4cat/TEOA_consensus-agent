from __future__ import annotations
import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class EntitySpec:
    name: str
    label: str
    category: str
    aliases: tuple[str, ...]


from shared.runtime.method_profile import method_profile

DEFAULT_ENTITY_SPECS: tuple[EntitySpec, ...] = tuple(
    (
        EntitySpec(row["name"], row["label"], row["category"], tuple(row["aliases"]))
        for row in method_profile()["entity_specs"]
    )
)
ENTITY_FREQUENCY_FIELDS = [
    "rank",
    "entity_name",
    "entity_label",
    "entity_category",
    "aliases",
    "chunk_mentions",
    "chunk_doc_count",
    "chunk_hit_count",
    "raw_claim_mentions",
    "raw_claim_doc_count",
    "raw_claim_hit_count",
    "structured_claim_mentions",
    "structured_claim_doc_count",
    "structured_claim_hit_count",
    "source_quote_mentions",
    "source_quote_doc_count",
    "source_quote_hit_count",
    "final_score",
    "top_doc_ids",
    "top_chunk_ids",
]


def build_entity_frequency_rows(
    chunk_rows: Iterable[dict[str, str]],
    raw_claim_rows: Iterable[dict[str, str]],
    *,
    entity_specs: tuple[EntitySpec, ...] = DEFAULT_ENTITY_SPECS,
) -> list[dict[str, object]]:
    if not entity_specs:
        from shared.selected_entity import require_entity

        selected = require_entity()
        entity_specs = (
            EntitySpec(
                selected.name, selected.name, "selected_entity", selected.aliases
            ),
        )
    matchers = {
        spec.name: _compile_alias_matcher(spec.aliases) for spec in entity_specs
    }
    stats = {spec.name: _new_stats() for spec in entity_specs}
    for row in chunk_rows:
        if (row.get("chunk_type") or "").strip() != "body_paragraph":
            continue
        text = row.get("clean_text") or row.get("text") or ""
        doc_id = row.get("doc_id", "")
        chunk_id = row.get("chunk_id", "")
        for spec in entity_specs:
            mentions = _count_mentions(matchers[spec.name], text)
            if mentions:
                _record_hit(stats[spec.name], "chunk", mentions, doc_id, chunk_id)
    for row in raw_claim_rows:
        doc_id = row.get("doc_id", "")
        chunk_id = row.get("chunk_id", "")
        candidate_text = row.get("raw_claim_text") or row.get("candidate_claim", "")
        source_quote = row.get("source_quote_exact") or row.get("source_quote", "")
        is_structured = _is_truthy(row.get("current_structured_pool_member", ""))
        for spec in entity_specs:
            raw_mentions = _count_mentions(matchers[spec.name], candidate_text)
            if raw_mentions:
                _record_hit(
                    stats[spec.name], "raw_claim", raw_mentions, doc_id, chunk_id
                )
                if is_structured:
                    _record_hit(
                        stats[spec.name],
                        "structured_claim",
                        raw_mentions,
                        doc_id,
                        chunk_id,
                    )
            quote_mentions = _count_mentions(matchers[spec.name], source_quote)
            if quote_mentions:
                _record_hit(
                    stats[spec.name], "source_quote", quote_mentions, doc_id, chunk_id
                )
    rows: list[dict[str, object]] = []
    spec_by_name = {spec.name: spec for spec in entity_specs}
    for name, stat in stats.items():
        if _total_mentions(stat) == 0:
            continue
        spec = spec_by_name[name]
        final_score = _score(stat)
        rows.append(
            {
                "rank": 0,
                "entity_name": spec.name,
                "entity_label": spec.label,
                "entity_category": spec.category,
                "aliases": "; ".join(spec.aliases),
                "chunk_mentions": stat["chunk_mentions"],
                "chunk_doc_count": len(stat["chunk_docs"]),
                "chunk_hit_count": stat["chunk_hits"],
                "raw_claim_mentions": stat["raw_claim_mentions"],
                "raw_claim_doc_count": len(stat["raw_claim_docs"]),
                "raw_claim_hit_count": stat["raw_claim_hits"],
                "structured_claim_mentions": stat["structured_claim_mentions"],
                "structured_claim_doc_count": len(stat["structured_claim_docs"]),
                "structured_claim_hit_count": stat["structured_claim_hits"],
                "source_quote_mentions": stat["source_quote_mentions"],
                "source_quote_doc_count": len(stat["source_quote_docs"]),
                "source_quote_hit_count": stat["source_quote_hits"],
                "final_score": final_score,
                "top_doc_ids": _top_keys(stat["doc_counter"]),
                "top_chunk_ids": _top_keys(stat["chunk_counter"]),
            }
        )
    rows.sort(
        key=lambda row: (
            float(row["final_score"]),
            int(row["chunk_doc_count"]),
            int(row["raw_claim_doc_count"]),
            int(row["chunk_mentions"]),
        ),
        reverse=True,
    )
    for index, row in enumerate(rows, start=1):
        row["rank"] = index
    return rows


def _compile_alias_matcher(aliases: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = [
        _alias_to_pattern(alias) for alias in sorted(aliases, key=len, reverse=True)
    ]
    return re.compile("|".join(alternatives), flags=re.IGNORECASE)


def _alias_to_pattern(alias: str) -> str:
    escaped = re.escape(alias)
    escaped = escaped.replace("\\ ", "\\s+")
    return f"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"


def _count_mentions(pattern: re.Pattern[str], text: str) -> int:
    return len(pattern.findall(text or ""))


def _new_stats() -> dict[str, object]:
    return {
        "chunk_mentions": 0,
        "chunk_docs": set(),
        "chunk_hits": 0,
        "raw_claim_mentions": 0,
        "raw_claim_docs": set(),
        "raw_claim_hits": 0,
        "structured_claim_mentions": 0,
        "structured_claim_docs": set(),
        "structured_claim_hits": 0,
        "source_quote_mentions": 0,
        "source_quote_docs": set(),
        "source_quote_hits": 0,
        "doc_counter": Counter(),
        "chunk_counter": Counter(),
    }


def _record_hit(
    stats: dict[str, object], prefix: str, mentions: int, doc_id: str, chunk_id: str
) -> None:
    stats[f"{prefix}_mentions"] += mentions
    stats[f"{prefix}_hits"] += 1
    if doc_id:
        stats[f"{prefix}_docs"].add(doc_id)
        stats["doc_counter"][doc_id] += mentions
    if chunk_id:
        stats["chunk_counter"][chunk_id] += mentions


def _is_truthy(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _total_mentions(stats: dict[str, object]) -> int:
    return (
        int(stats["chunk_mentions"])
        + int(stats["raw_claim_mentions"])
        + int(stats["structured_claim_mentions"])
        + int(stats["source_quote_mentions"])
    )


def _score(stats: dict[str, object]) -> float:
    score = (
        int(stats["chunk_mentions"])
        + int(stats["raw_claim_mentions"]) * 2
        + int(stats["structured_claim_mentions"]) * 3
        + int(stats["source_quote_mentions"])
        + len(stats["chunk_docs"]) * 5
        + len(stats["raw_claim_docs"]) * 8
        + len(stats["structured_claim_docs"]) * 12
    )
    return round(float(score), 3)


def _top_keys(counter: Counter[str], limit: int = 8) -> str:
    return "; ".join((key for key, _count in counter.most_common(limit)))
