"""Bind metadata to original first-page Content List evidence.

This module is deliberately independent of body-only fulltext extraction. It performs
no file/network/model calls, never changes document type from research topic, and
leaves ambiguous metadata unresolved. Callers should retain this returned provenance.
"""

from __future__ import annotations
import html
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

_FORMATTING = re.compile(
    "\\\\(?:mathrm|mathbf|mathsf|mathit|text|textbf|textrm|operatorname)\\s*"
)
_DOI = re.compile("10\\.\\d{4,9}\\s*/\\s*[-._;()/:A-Z0-9]+", re.I)
_SECTION = re.compile(
    "^(?:\\d+(?:\\.\\d+)*[.)]?\\s*)?(?:abstract|introduction|references|conclusions?|results(?: and discussion)?|discussion|experimental(?: section| details)?|materials(?: and methods)?|methods|acknowledg(?:e)?ments?|supporting information|supplementary information|graphical abstract|graphitic abstract|table of contents|context\\s*(?:&|and)\\s*scale|contents|keywords?|highlights?|just accepted|accepted manuscript|author manuscript|accepted article|akzeptierter artikel|journal pre-proofs?|article|research article|research paper|review article|review|edge article|article info|author contributions|advisory board:?|executive advisory board:?|international advisory board:?|editorial advisory board:?|advisory board chairmen:?|editors:?|communication|perspective|letter|open access|open|check for updates)$",
    re.I,
)
_JOURNAL = re.compile(
    "^(?:advanced (?:science|materials|energy materials|functional materials)|nature(?: communications| chemistry| catalysis| materials| energy)?|science(?: advances)?|acs applied materials\\s*(?:&|and)\\s*interfaces|journal of (?:the american chemical society|materials chemistry [abc])|applied catalysis [ab](?::? environmental)?|chemical (?:science|reviews)|angewandte(?: chemie)?(?: international edition)?|energy\\s*(?:&|and)\\s*environmental science|european journal of inorganic chemistry|chemistry europe|chemie|chemcatchem|eurjic)$",
    re.I,
)
_FRONTMATTER = {
    "acceptedmanuscript",
    "authormanuscript",
    "authorsacceptedmanuscript",
    "acceptedarticle",
    "akzeptierterartikel",
    "journalpreproof",
    "journalpreproofs",
    "justaccepted",
}


def _piece_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join((_piece_text(item) for item in value))
    if isinstance(value, dict):
        return _piece_text(value.get("content", value.get("item_content", "")))
    return ""


def _balanced(text: str, start: int) -> tuple[str, int]:
    if start >= len(text) or text[start] != "{":
        return ("", start)
    depth = 0
    for end in range(start, len(text)):
        if text[end] == "{":
            depth += 1
        elif text[end] == "}":
            depth -= 1
            if depth == 0:
                return (text[start + 1 : end], end + 1)
    return ("", start)


def _title_segments(text: str) -> list[tuple[str, bool]]:
    """Retain explicit math spans so only their OCR O/0 can be assisted by an index."""
    text = html.unescape(text)
    segments: list[tuple[str, bool]] = []
    pos = 0
    while pos < len(text):
        match = _FORMATTING.search(text, pos)
        if not match:
            segments.append((text[pos:], False))
            break
        segments.append((text[pos : match.start()], False))
        content, end = _balanced(text, match.end())
        if end == match.end():
            segments.append((text[match.start() : match.end()], False))
            pos = match.end()
            continue
        while True:
            sub = re.match("\\s*[_^]\\s*", text[end:])
            if not sub:
                break
            substart = end + sub.end()
            subcontent, subend = _balanced(text, substart)
            if subend == substart:
                atom = re.match("[A-Za-z0-9+-]", text[substart:])
                if not atom:
                    break
                subcontent, subend = (atom.group(), substart + atom.end())
            content += subcontent
            end = subend
        content = _FORMATTING.sub("", content)
        content = re.sub("\\\\([A-Za-z]+)", "\\1", content)
        content = re.sub("[{}_^]", "", content)
        content = re.sub("\\s+", "", content)
        segments.append((content, True))
        pos = end
    return segments


def _normal_and_flags(text: str) -> tuple[str, list[bool]]:
    chars: list[str] = []
    flags: list[bool] = []
    for segment, is_math in _title_segments(text):
        segment = re.sub("<[^>]+>", "", segment)
        segment = unicodedata.normalize("NFKC", segment).casefold()
        for char in segment:
            if char.isalnum():
                chars.append(char)
                flags.append(is_math)
    return ("".join(chars), flags)


def _normal(text: str) -> str:
    return _normal_and_flags(text)[0]


def _display_title(text: str) -> str:
    output = ""
    prior_math = False
    for segment, is_math in _title_segments(text):
        if is_math:
            if prior_math:
                output = output.rstrip()
            output += segment
        else:
            segment = re.sub("<[^>]+>", "", segment)
            if prior_math and segment and segment[0].isalpha():
                output += " "
            output += segment
        if segment.strip():
            prior_math = is_math
    output = unicodedata.normalize("NFKC", output)
    return re.sub("\\s+", " ", output).strip()


def _formula_ocr_only(raw: str, proposed: str) -> bool:
    left, math_flags = _normal_and_flags(raw)
    right = _normal(proposed)
    if not left or SequenceMatcher(None, left, right).ratio() < 0.985:
        return False
    changes = 0
    for tag, a, b, c, d in SequenceMatcher(None, left, right).get_opcodes():
        if tag == "equal":
            continue
        if tag != "replace" or b - a != d - c or (not all(math_flags[a:b])):
            return False
        if any(({x, y} != {"0", "o"} for x, y in zip(left[a:b], right[c:d]))):
            return False
        changes += b - a
    return 0 < changes <= 2


def _first_page_blocks(value: Any, page_offset: int = 0) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        return []
    v2 = isinstance(value[0], list)
    if v2:
        if page_offset >= len(value):
            return []
        entries = [
            (page_offset, i, block, f"/{page_offset}/{i}")
            for i, block in enumerate(value[page_offset])
        ]
    else:
        raw = [(i, block) for i, block in enumerate(value) if isinstance(block, dict)]
        numeric_pages = [b.get("page_idx", b.get("page_index", 0)) for _, b in raw]
        numeric_pages = [p for p in numeric_pages if isinstance(p, int)]
        pages = sorted(set(numeric_pages)) or [0]
        if page_offset >= len(pages):
            return []
        first = pages[page_offset]
        entries = [
            (first, i, b, f"/{i}")
            for i, b in raw
            if b.get("page_idx", b.get("page_index", first)) == first
        ]
    result = []
    for page, number, block, pointer in entries:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type", ""))
        if kind in {"image", "figure"}:
            continue
        content = block.get("content")
        if v2 and isinstance(content, dict):
            keys = [
                k
                for k in content
                if k.endswith("_content")
                or k in {"list_items", "table_caption", "table_footnote"}
            ]
            fields = [(key, _piece_text(content[key])) for key in keys]
            level = content.get("level")
        else:
            fields = [("text", str(block.get("text") or ""))]
            level = block.get("text_level")
        for field, text in fields:
            text = text.strip()
            if not text:
                continue
            result.append(
                {
                    "page_index": page,
                    "page_number": page + 1,
                    "block_index": number,
                    "block_type": kind,
                    "field": field,
                    "json_pointer": pointer + ("/content/" if v2 else "/") + field,
                    "bbox": block.get("bbox", []),
                    "level": level,
                    "raw": text,
                    "is_title": kind == "title"
                    or (
                        kind == "text"
                        and isinstance(level, (int, float))
                        and (level > 0)
                    ),
                }
            )
    return result


def _plausible_title(text: str, *, allow_short: bool = False) -> bool:
    display = _display_title(text)
    normal = _normal(text)
    words = re.findall("[^\\W\\d_]{2,}", display, flags=re.UNICODE)
    if (
        len(normal) < (6 if allow_short else 24)
        or len(words) < (1 if allow_short else 4)
        or len(display) > 550
    ):
        return False
    if (
        _SECTION.fullmatch(display)
        or _JOURNAL.fullmatch(display)
        or normal in {"abstract", "articleinfo", "introduction", "researchpaper"}
    ):
        return False
    if re.match(
        "^(?:doi\\b|https?://|www\\.|received\\b|accepted\\b|published\\b|copyright\\b|©|correspondence\\b|keywords?\\b)",
        display,
        re.I,
    ):
        return False
    if re.match(
        "^(?:this article can be cited|this is an accepted manuscript|accepted manuscripts are|revised manuscript (?:id|number)|cutting-edge research for)",
        display,
        re.I,
    ):
        return False
    if re.search("\\bDOI\\s*:\\s*10\\.", display, re.I):
        return False
    if re.search("\\b[\\w.+-]+@[\\w.-]+\\.[A-Za-z]{2,}\\b", display) or re.search(
        "\\b(?:department|university|institute|advisory board)\\b", display, re.I
    ):
        return False
    if _DOI.fullmatch(display):
        return False
    return True


def _title_candidates(blocks: list[dict[str, Any]], hints: str) -> list[dict[str, Any]]:
    boundary = next(
        (
            b["block_index"]
            for b in blocks
            if b["is_title"]
            and re.sub("^\\d+", "", _normal(b["raw"]))
            in {"abstract", "introduction", "summary"}
            or (b["block_type"] in {"paragraph", "text"} and len(b["raw"]) > 700)
        ),
        None,
    )
    early = [b for b in blocks if boundary is None or b["block_index"] < boundary]
    page_text = "\n".join((b["raw"] for b in blocks))
    has_biblio_short_title = any(
        (_normal(b["raw"]) == "abstract" for b in blocks)
    ) and bool(_DOI.search(page_text))
    candidates = [
        b
        for b in early
        if b["is_title"]
        and _plausible_title(
            b["raw"],
            allow_short=bool(
                hints
                and _normal(b["raw"]) == _normal(hints)
                or (has_biblio_short_title and b["block_index"] <= 5)
            ),
        )
    ]
    for b in early:
        if b["block_type"] not in {"paragraph", "text"}:
            continue
        label = re.match("^\\s*(?:Title|Titel)\\s*:\\s*(.+)$", b["raw"], re.I | re.S)
        if label and _plausible_title(label.group(1), allow_short=True):
            candidates.append(
                dict(
                    b,
                    raw=label.group(1),
                    block_raw=b["raw"],
                    character_range=[label.start(1), label.end(1)],
                    explicit_title_field=True,
                )
            )
        elif (
            hints
            and _normal(b["raw"]) == _normal(hints)
            and _plausible_title(b["raw"], allow_short=True)
            and (not any((c["block_index"] < b["block_index"] for c in candidates)))
        ):
            candidates.append(dict(b, source_index_verified_standalone_block=True))
    marker = next((b for b in early if _normal(b["raw"]) in _FRONTMATTER), None)
    if marker:
        after = [
            b
            for b in early
            if marker["block_index"] < b["block_index"] <= marker["block_index"] + 3
            and b["block_type"] in {"paragraph", "text"}
        ]
        for b in after:
            if _normal(b["raw"]) in {
                "researchpaper",
                "article",
                "researcharticle",
                "review",
            }:
                continue
            if _plausible_title(b["raw"]):
                candidates.append(dict(b, accepted_manuscript_title_position=True))
            break
    titles = [b for b in early if b["is_title"]]
    for start in range(len(titles)):
        for width in (2, 3, 4):
            group = titles[start : start + width]
            if len(group) != width or any(
                (
                    right["block_index"] != left["block_index"] + 1
                    for left, right in zip(group, group[1:])
                )
            ):
                continue
            joined = " ".join((b["raw"] for b in group))
            if (
                hints
                and _normal(joined) == _normal(hints)
                and _plausible_title(joined, allow_short=True)
            ):
                candidates.append(
                    dict(
                        group[0],
                        raw=joined,
                        component_anchors=group,
                        joined_title_blocks=True,
                    )
                )
    unique = {}
    for candidate in candidates:
        key = (candidate["page_index"], candidate["block_index"], candidate["raw"])
        unique[key] = candidate
    return list(unique.values())


def _publisher_frontmatter(blocks: list[dict[str, Any]]) -> bool:
    text = "\n".join((b["raw"] for b in blocks))
    if any(
        (
            _normal(b["raw"]) in _FRONTMATTER
            or _normal(b["raw"]).startswith("acceptedmanuscript")
            for b in blocks
        )
    ):
        return True
    if re.search("(?:Title|Titel)\\s*:", text, re.I):
        return True
    return bool(
        re.search(
            "\\b(?:ISSN|ISBN|Front Cover|Inside Cover|Volume\\s+\\d+|Part\\s+(?:One|Two|Three|Four|\\d+))\\b",
            text,
            re.I,
        )
    )


def _article_front_page(blocks: list[dict[str, Any]], hints: str) -> bool:
    text = "\n".join((b["raw"] for b in blocks))
    candidates = _title_candidates(blocks, hints)
    if not candidates:
        return False
    if hints and any((_normal(b["raw"]) == _normal(hints) for b in candidates)):
        return True
    has_abstract = bool(
        re.search(
            "(?:^|\\n)\\s*(?:A\\s*B\\s*S\\s*T\\s*R\\s*A\\s*C\\s*T|Abstract)\\s*[:\\n]",
            text,
            re.I,
        )
    )
    has_biblio = bool(
        re.search("(?:\\bDOI\\s*:|doi\\.org/|Cite this:|Cite as:)", text, re.I)
    )
    has_dates = bool(re.search("\\b(?:Received|Accepted|Published)\\b", text))
    return has_abstract or (has_biblio and has_dates)


def _source_doi(value: Any) -> str:
    text = html.unescape(str(value or "")).strip().lower()
    return re.sub("^https?://(?:dx\\.)?doi\\.org/", "", text).rstrip(".,;")


def extract_source_metadata(
    value: Any, source_row: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return metadata plus source anchors; unresolved values are empty strings.

    source_row title/doi are comparison hints, never independent authority.
    `doi_candidates` always retains conflicting first-page identifiers. A unique
    clear primary DOI can replace a discordant index identifier with a flag; close
    OCR-like conflicts and multiple identifiers remain unresolved.
    """
    row = source_row or {}
    blocks = _first_page_blocks(value)
    first_page_text = "\n".join((block["raw"] for block in blocks))
    flags: list[str] = []
    hints = str(row.get("title") or "").strip()
    candidates = _title_candidates(blocks, hints)
    metadata_blocks = list(blocks)
    metadata_page_numbers = [blocks[0]["page_number"]] if blocks else []
    if _publisher_frontmatter(blocks):
        for offset in (1, 2):
            later = _first_page_blocks(value, offset)
            if _article_front_page(later, hints):
                metadata_blocks.extend(later)
                candidates.extend(_title_candidates(later, hints))
                metadata_page_numbers.append(later[0]["page_number"])
                flags.append("publisher_frontmatter_followed_by_article_metadata_page")
                break
    chosen = None
    exact = [b for b in candidates if hints and _normal(b["raw"]) == _normal(hints)]
    if exact:
        chosen = exact[0]
    elif candidates:
        first_candidate_page = min((b["page_index"] for b in candidates))
        first_candidates = [
            b for b in candidates if b["page_index"] == first_candidate_page
        ]
        close = sorted(
            candidates,
            key=lambda b: SequenceMatcher(
                None, _normal(b["raw"]), _normal(hints)
            ).ratio(),
            reverse=True,
        )
        if (
            hints
            and SequenceMatcher(None, _normal(close[0]["raw"]), _normal(hints)).ratio()
            >= 0.95
        ):
            chosen = close[0]
        elif len({_normal(b["raw"]) for b in candidates}) == 1:
            chosen = candidates[0]
        elif len(first_candidates) == 1:
            chosen = first_candidates[0]
        elif any((b.get("explicit_title_field") for b in candidates)):
            labeled = [b for b in candidates if b.get("explicit_title_field")]
            if len({_normal(b["raw"]) for b in labeled}) == 1:
                chosen = labeled[0]
        else:
            longest = sorted(
                candidates, key=lambda b: len(_normal(b["raw"])), reverse=True
            )
            if len(_normal(longest[0]["raw"])) >= 1.7 * len(_normal(longest[1]["raw"])):
                chosen = longest[0]
                flags.append("title_selected_from_multiple_headers_by_length")
            else:
                flags.append("ambiguous_first_page_title_candidates")
    title = ""
    title_raw = ""
    title_source = "unresolved"
    title_index_assistance: dict[str, Any] = {}
    if chosen:
        title_raw = chosen["raw"]
        title = _display_title(title_raw)
        if chosen.get("explicit_title_field"):
            title_source = "source_bibliographic_Title_field"
        elif chosen.get("accepted_manuscript_title_position"):
            title_source = "first_page_accepted_manuscript_title"
        elif chosen.get("source_index_verified_standalone_block"):
            title_source = "source_first_page_standalone_block"
        else:
            title_source = "first_page_title_block"
        if blocks and chosen["page_number"] != blocks[0]["page_number"]:
            title_source = (
                "article_metadata_page_"
                + str(chosen["page_number"])
                + ":"
                + title_source
            )
        if hints and _normal(hints) == _normal(title_raw):
            title = _display_title(hints)
            title_source += "+source_index_exact_normalized_match"
            title_index_assistance = {
                "kind": "formatting_only",
                "source_index_title": hints,
                "normalized_match": True,
            }
        elif hints and _formula_ocr_only(title_raw, hints):
            title = _display_title(hints)
            flags.append("title_math_ocr_O0_assisted_by_high_matching_index")
            title_source += "+flagged_formula_ocr_assistance"
            title_index_assistance = {
                "kind": "explicit_math_O0_ocr",
                "source_index_title": hints,
                "raw_title_preserved": True,
            }
        elif hints:
            flags.append("source_index_title_not_adopted")
    else:
        flags.append("unresolved_title")
    source_doi = _source_doi(row.get("doi"))
    doi_anchors: list[dict[str, Any]] = []
    related_boundary: dict[int, int] = {}
    for block in metadata_blocks:
        if (
            re.search(
                "ARTICLES YOU MAY BE INTERESTED IN|RELATED ARTICLES|REFERENCES",
                block["raw"],
                re.I,
            )
            and len(block["raw"]) < 90
        ):
            related_boundary[block["page_index"]] = block["block_index"]
    excluded_doi_anchors: list[dict[str, Any]] = []
    for block in metadata_blocks:
        text = html.unescape(re.sub("<[^>]+>", "", block["raw"]))
        marker = bool(re.search("(?:\\bdoi\\s*:|(?:dx\\.)?doi\\.org/)", text, re.I))
        obvious_metadata = block["block_type"] in {"page_header", "page_footer"} or (
            marker
            and (
                len(text) <= 220
                or bool(
                    re.match(
                        "^(?:DOI\\s*:|https?://(?:dx\\.)?doi\\.org|doi\\.org|How to cite|Please cite this article|To be cited as|Cite as:|Cite this:)",
                        text,
                        re.I,
                    )
                )
            )
        )
        if not obvious_metadata:
            continue
        hits = []
        for match in _DOI.finditer(text):
            candidate = re.sub("\\s+", "", match.group()).lower().rstrip(".,;:")
            while candidate.endswith(")") and candidate.count(")") > candidate.count(
                "("
            ):
                candidate = candidate[:-1]
            hits.append(candidate)
        compact_text = re.sub("\\s+", "", text).lower()
        if source_doi and source_doi in compact_text:
            hits = [
                h for h in hits if not (h != source_doi and source_doi.startswith(h))
            ]
            hits.append(source_doi)
        for candidate in dict.fromkeys(hits):
            role = "document"
            if re.search(
                "creative commons|creativecommons|licen[sc]e|figshare|zenodo|dryad|data[ -]?set|data deposit",
                text,
                re.I,
            ):
                role = "license_or_dataset_context"
            elif re.search(
                "\\b(?:in article number|the cover|cover image|cover picture|more details can be found)\\b",
                text,
                re.I,
            ):
                role = "cover_cited_article"
            elif block["block_index"] > related_boundary.get(
                block["page_index"], 10**9
            ) and block["block_type"] not in {"page_header", "page_footer"}:
                role = "related_article_or_reference"
            anchor = dict(block, doi=candidate, doi_role=role)
            if role == "document":
                doi_anchors.append(anchor)
            else:
                excluded_doi_anchors.append(anchor)
    observed_dois = {a["doi"] for a in doi_anchors}
    for anchor in list(doi_anchors):
        prefixes = [d for d in observed_dois if anchor["doi"] == d + "acs"]
        if len(prefixes) == 1:
            doi_anchors.remove(anchor)
            excluded_doi_anchors.append(
                dict(
                    anchor,
                    doi_role="publisher_name_concatenated_after_independently_anchored_doi",
                )
            )
    doi_candidates = sorted({a["doi"] for a in doi_anchors})
    doi = ""
    doi_source = "unresolved"
    if len(doi_candidates) == 1:
        candidate = doi_candidates[0]
        if source_doi and candidate != source_doi:
            flags.append("source_index_doi_conflict")
            similarity = SequenceMatcher(None, candidate, source_doi).ratio()
            if similarity >= 0.94:
                flags.append("doi_close_conflict_possible_ocr_unresolved")
            else:
                doi = candidate
                doi_source = "first_page_document_doi+source_index_conflict"
        else:
            doi = candidate
            doi_source = "first_page_document_doi"
    elif len(doi_candidates) > 1:
        flags.append("multiple_first_page_document_dois_unresolved")
    if excluded_doi_anchors:
        flags.append("non_document_or_malformed_doi_observations_preserved")
    if not doi:
        flags.append("unresolved_doi")
    if re.search(
        "\\b(?:the cover shows|on the cover|cover image|cover picture|cover illustration)\\b",
        first_page_text,
        re.I,
    ):
        flags.append("cover_description_present_review_eligibility")
    type_anchors = []
    for block in metadata_blocks:
        label = (
            re.sub("\\s+", " ", re.sub("<[^>]+>", "", block["raw"])).strip().casefold()
        )
        if label in {
            "research article",
            "article",
            "review",
            "review article",
            "minireview",
            "mini review",
        }:
            type_anchors.append(
                dict(block, doc_type="review" if "review" in label else "research")
            )
    types = {a["doc_type"] for a in type_anchors}
    doc_type = next(iter(types)) if len(types) == 1 else ""
    if len(types) > 1:
        flags.append("conflicting_first_page_document_type_labels")
    return {
        "title": title,
        "canonical_title": title,
        "doi": doi,
        "title_raw": title_raw,
        "title_source": title_source,
        "doi_source": doi_source,
        "doc_type": doc_type,
        "doc_type_source": "explicit_first_page_label" if doc_type else "",
        "first_page_text": first_page_text,
        "first_page_number": blocks[0]["page_number"] if blocks else None,
        "metadata_page_numbers": metadata_page_numbers,
        "article_first_page_text": "\n".join(
            (
                b["raw"]
                for b in metadata_blocks
                if chosen and b["page_index"] == chosen["page_index"]
            )
        ),
        "source_index_title": hints,
        "source_index_doi": source_doi,
        "source_index_title_assistance": title_index_assistance,
        "doi_candidates": doi_candidates,
        "anchors": {
            "title": [chosen] if chosen else [],
            "doi": doi_anchors,
            "excluded_doi_observations": excluded_doi_anchors,
            "doc_type": type_anchors,
        },
        "title_candidates": candidates,
        "flags": list(dict.fromkeys(flags)),
    }
