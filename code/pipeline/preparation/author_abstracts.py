from __future__ import annotations
import json,re
from pathlib import Path

AUTHOR_ABSTRACT_FIELDS = [
    "doc_id",
    "abstract_text",
    "abstract_status",
    "abstract_extraction_method",
    "abstract_source",
    "abstract_block_count",
]


def _extract_author_abstract_row(
    *,
    doc_id: str,
    corpus_row: dict[str, str],
    mineru_dir: Path,
) -> dict[str, str]:
    filename = str(corpus_row.get("filename", "")).strip()
    json_path = mineru_dir / filename if filename else Path()
    if not json_path.exists():
        candidates = list(mineru_dir.glob(f"{doc_id}*.json"))
        json_path = candidates[0] if candidates else json_path
    if not json_path.exists():
        return _abstract_row(doc_id, "", "missing_mineru_json", "", 0)

    try:
        blocks = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _abstract_row(doc_id, "", "invalid_mineru_json", str(json_path), 0)

    text_blocks = _text_blocks(blocks)
    abstract_method = "explicit_abstract_heading"
    abstract_text, block_count = _extract_abstract_from_blocks(text_blocks)
    if not abstract_text:
        abstract_method = "unlabeled_pre_introduction"
        abstract_text, block_count = _extract_unlabeled_pre_introduction_abstract(
            text_blocks
        )
    if not abstract_text:
        abstract_method = "nature_front_matter_summary"
        abstract_text, block_count = _extract_nature_front_matter_summary(text_blocks)
    if not abstract_text:
        abstract_method = "rsc_communication_summary"
        abstract_text, block_count = _extract_rsc_communication_summary(text_blocks)
    if not abstract_text:
        abstract_method = "wiley_communication_lead_summary"
        abstract_text, block_count = _extract_wiley_communication_lead_summary(
            text_blocks
        )
    if not abstract_text:
        abstract_method = "no_section_front_matter_lead_summary"
        abstract_text, block_count = _extract_no_section_front_matter_lead_summary(
            text_blocks
        )
    if not abstract_text:
        return _abstract_row(
            doc_id, "", "missing_author_abstract_region", "", str(json_path), 0
        )
    return _abstract_row(
        doc_id, abstract_text, "found", abstract_method, str(json_path), block_count
    )


def _text_blocks(blocks: object) -> list[dict[str, object]]:
    if isinstance(blocks, dict):
        for value in blocks.values():
            if isinstance(value, list):
                blocks = value
                break
    if not isinstance(blocks, list):
        return []
    output: list[dict[str, object]] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = " ".join(str(block.get("text", "")).split()).strip()
        if text:
            output.append({"text": text, "page_idx": int(block.get("page_idx") or 0)})
    return output


def _extract_abstract_from_blocks(blocks: list[dict[str, object]]) -> tuple[str, int]:
    start_index = -1
    first_text = ""
    for idx, block in enumerate(blocks):
        text = str(block["text"])
        if _is_abstract_heading(text):
            start_index = idx + 1
            first_text = _text_after_abstract_label(text)
            break
    if start_index < 0:
        return "", 0

    collected: list[str] = []
    if first_text:
        collected.append(first_text)
    for block in blocks[start_index:]:
        text = str(block["text"])
        if _is_abstract_stop_heading(text):
            break
        if _looks_like_page_chrome(text):
            continue
        collected.append(text)
        if len(" ".join(collected)) >= 2500:
            break
    abstract = " ".join(collected).strip()
    return abstract[:2500], len(collected)


def _extract_unlabeled_pre_introduction_abstract(
    blocks: list[dict[str, object]],
) -> tuple[str, int]:
    intro_idx = -1
    for idx, block in enumerate(blocks[:80]):
        if _is_introduction_heading(str(block["text"])):
            intro_idx = idx
            break
    if intro_idx <= 0:
        return "", 0

    candidates: list[str] = []
    for block in blocks[:intro_idx]:
        text = str(block["text"]).strip()
        if _is_author_abstract_candidate(text):
            candidates.append(text)
    if not candidates:
        return "", 0

    # In unlabeled Wiley/RSC-style layouts, the author abstract is usually the
    # first long scientific paragraph between title/authors and Introduction.
    selected = candidates[:2]
    abstract = " ".join(selected).strip()
    return abstract[:2500], len(selected)


def _extract_nature_front_matter_summary(
    blocks: list[dict[str, object]],
) -> tuple[str, int]:
    if not _looks_like_nature_article(blocks):
        return "", 0
    for block in blocks[:14]:
        text = str(block["text"]).strip()
        if int(block.get("page_idx") or 0) > 0:
            break
        if _is_author_abstract_candidate(text):
            return text[:2500], 1
    return "", 0


def _extract_rsc_communication_summary(
    blocks: list[dict[str, object]],
) -> tuple[str, int]:
    for idx, block in enumerate(blocks[:80]):
        if _normalize_heading(str(block["text"])) != "communication":
            continue
        doi_idx = -1
        search_end = min(len(blocks), idx + 14)
        for probe_idx in range(idx + 1, search_end):
            lowered = str(blocks[probe_idx]["text"]).lower()
            if lowered.startswith("doi:") or "www.rsc.org" in lowered:
                doi_idx = probe_idx
        if doi_idx < 0:
            continue
        for candidate in blocks[doi_idx + 1 : min(len(blocks), doi_idx + 8)]:
            text = str(candidate["text"]).strip()
            if _is_author_abstract_candidate(text):
                return text[:2500], 1
    return "", 0


def _extract_wiley_communication_lead_summary(
    blocks: list[dict[str, object]],
) -> tuple[str, int]:
    if not _looks_like_wiley_communication(blocks):
        return "", 0

    candidates: list[str] = []
    for block in blocks[2:20]:
        text = str(block["text"]).strip()
        if _looks_like_author_affiliation_start(text):
            break
        if _is_author_abstract_candidate(text):
            candidates.append(text)
        if len(candidates) >= 3:
            break
    if not candidates:
        return "", 0

    abstract = " ".join(candidates).strip()
    return abstract[:2500], len(candidates)


def _extract_no_section_front_matter_lead_summary(
    blocks: list[dict[str, object]],
) -> tuple[str, int]:
    if not _looks_like_no_section_short_article(blocks):
        return "", 0

    candidates: list[str] = []
    for block in blocks[2:12]:
        text = str(block["text"]).strip()
        if _is_abstract_stop_heading(text) or _looks_like_author_affiliation_start(
            text
        ):
            break
        if _is_author_abstract_candidate(text):
            candidates.append(text)
        if len(candidates) >= 3:
            break
    if not candidates:
        return "", 0
    abstract = " ".join(candidates).strip()
    return abstract[:2500], len(candidates)


def _looks_like_nature_article(blocks: list[dict[str, object]]) -> bool:
    front_text = " ".join(str(block["text"]) for block in blocks[:12]).lower()
    return (
        "10.1038/s41467" in front_text
        or ("published online" in front_text and "check for updates" in front_text)
        or (
            "article" in front_text and "open" in front_text and "10.1038" in front_text
        )
    )


def _looks_like_wiley_communication(blocks: list[dict[str, object]]) -> bool:
    if len(blocks) < 4:
        return False
    first_page = [
        str(block["text"]).lower()
        for block in blocks[:24]
        if int(block.get("page_idx") or 0) == 0
    ]
    front_text = " ".join(first_page)
    has_wiley_marker = any(
        marker in front_text
        for marker in (
            "supporting information for this artic",
            "angew. chem",
            "adv. mater",
            "chem. asian j",
        )
    )
    has_affiliation_marker = any(
        marker in front_text
        for marker in (
            "e-mail:",
            "department of",
            "university",
            "[\\*]",
            "prof.",
            "dr.",
        )
    )
    return has_wiley_marker or has_affiliation_marker


def _looks_like_no_section_short_article(blocks: list[dict[str, object]]) -> bool:
    front_blocks = blocks[:40]
    has_exp_section = any(
        _normalize_heading(str(block["text"])) == "experimental section"
        for block in front_blocks
    )
    has_intro = any(
        _is_introduction_heading(str(block["text"])) for block in front_blocks
    )
    has_title_author_lead = (
        len(blocks) >= 5
        and len(str(blocks[0]["text"])) >= 60
        and len(str(blocks[1]["text"])) <= 180
        and _is_author_abstract_candidate(str(blocks[2]["text"]))
    )
    return has_exp_section and not has_intro and has_title_author_lead


def _looks_like_author_affiliation_start(text: str) -> bool:
    lowered = text.lower()
    return (
        lowered.startswith(("[\\*]", "[*]", "dr.", "prof."))
        or lowered.startswith("department of")
        or "e-mail:" in lowered
        or "supporting information for this artic" in lowered
    )


def _is_abstract_heading(text: str) -> bool:
    lowered = text.strip().lower()
    if lowered.startswith("graphical abstract"):
        return False
    return bool(re.match(r"^abstract\b\s*[:.]?", lowered))


def _text_after_abstract_label(text: str) -> str:
    return re.sub(r"^\s*abstract\s*[:.]?\s*", "", text, flags=re.IGNORECASE).strip()


def _is_abstract_stop_heading(text: str) -> bool:
    normalized = _normalize_heading(text)
    return normalized in {
        "keywords",
        "keyword",
        "introduction",
        "1 introduction",
        "background",
        "results",
        "results and discussion",
        "experimental",
    }


def _is_introduction_heading(text: str) -> bool:
    normalized = _normalize_heading(text)
    return normalized in {"introduction", "1 introduction"}


def _normalize_heading(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())


def _is_author_abstract_candidate(text: str) -> bool:
    compact = " ".join(text.split()).strip()
    if len(compact) < 220:
        return False
    lowered = compact.lower()
    if _looks_like_page_chrome(compact):
        return False
    if any(
        marker in lowered
        for marker in ("e-mail", "email", "doi:", "orcid", "supporting information")
    ):
        return False
    if (
        re.search(r"\b(abstract|introduction|keywords?)\b", lowered)
        and len(compact) < 400
    ):
        return False
    scientific_markers = (
        "photocatal",
        "co2",
        "carbon dioxide",
        "hydrogen",
        "catalyst",
        "reduction",
        "framework",
        "visible light",
        "selectivity",
        "reaction",
    )
    return any(marker in lowered for marker in scientific_markers)


def _looks_like_page_chrome(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "view article online",
            "check for updates",
            "cite this",
            "downloaded by",
            "published on",
            "journal name",
        )
    )


def _abstract_row(
    doc_id: str,
    abstract: str,
    status: str,
    method: str,
    source: str,
    block_count: int,
) -> dict[str, str]:
    return {
        "doc_id": doc_id,
        "abstract_text": abstract,
        "abstract_status": status,
        "abstract_extraction_method": method,
        "abstract_source": source,
        "abstract_block_count": str(block_count),
    }

