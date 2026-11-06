from __future__ import annotations

import html
import re
import time
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from shared.pdf_utils import extract_doi


_USER_AGENT = "ClaimFrequencyPipeline/1.0 (reference enrichment)"
_CROSSREF_WORKS_URL = "https://api.crossref.org/works"
_OPENALEX_WORKS_URL = "https://api.openalex.org/works/https://doi.org/{doi}"
_OPENALEX_SEARCH_URL = "https://api.openalex.org/works"
_SEMANTIC_SCHOLAR_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
_SEMANTIC_SCHOLAR_PAPER_URL = "https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
_EUROPEPMC_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_DATACITE_DOIS_URL = "https://api.datacite.org/dois"
_UNPAYWALL_WORK_URL = "https://api.unpaywall.org/v2/{doi}"
_CROSSREF_WORK_BY_DOI_URL = "https://api.crossref.org/works/{doi}"
_ABSTRACT_EXCERPT_LIMIT = 500
_PDF_SIGNATURE_SCAN_LIMIT = 1024
_CROSSREF_ROWS = 10
_FALLBACK_ROWS = 5
_NON_FILENAME_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")
_PAGE_TOKEN_RE = re.compile(r"\b\d{2,}\b")
_STOPWORDS = {
    "a",
    "an",
    "and",
    "for",
    "from",
    "in",
    "of",
    "on",
    "paper",
    "the",
    "to",
    "with",
}
_CROSSREF_NON_ARTICLE_TITLE_RE = re.compile(
    r"\b(?:graphical\s+abstract|masthead|cover\s+(?:picture|profile|feature)|"
    r"frontispiece|inside\s+cover)\b",
    re.IGNORECASE,
)


def enrich_reference(reference_text: str, pdf_dir: Path) -> dict[str, str]:
    normalized_reference_text = str(reference_text or "").strip()
    if not normalized_reference_text:
        return _build_result(
            reference_text="",
            identifier_type="",
            identifier_value="",
            download_status="no_identifier",
            pdf_path="",
            abstract_excerpt="",
        )
    lookup_metadata = (
        _metadata_result(
            identifier_type="doi",
            identifier_value=extract_doi(normalized_reference_text),
            identifier_source="reference_text",
            lookup_status="doi_in_reference",
            abstract_excerpt="",
        )
        if extract_doi(normalized_reference_text)
        else _lookup_reference_metadata(normalized_reference_text)
    )
    doi = lookup_metadata.get("identifier_value", "")
    if not doi:
        return _build_result(
            reference_text=normalized_reference_text,
            identifier_type="",
            identifier_value="",
            download_status="no_identifier",
            pdf_path="",
            abstract_excerpt="",
        )

    work = _fetch_openalex_work(doi)
    openalex_abstract = _extract_abstract_excerpt(work)
    if openalex_abstract and not _looks_like_page_chrome(openalex_abstract):
        return _build_result(
            reference_text=normalized_reference_text,
            identifier_type="doi",
            identifier_value=doi,
            download_status="abstract_only",
            pdf_path="",
            abstract_excerpt=openalex_abstract,
        )

    fetched_abstract = fetch_reference_abstract(doi, normalized_reference_text)
    abstract_excerpt = fetched_abstract.get("abstract_excerpt", "")
    if abstract_excerpt:
        return _build_result(
            reference_text=normalized_reference_text,
            identifier_type="doi",
            identifier_value=doi,
            download_status="abstract_only",
            pdf_path="",
            abstract_excerpt=abstract_excerpt,
        )

    abstract_excerpt = lookup_metadata.get("abstract_excerpt", "")
    if abstract_excerpt:
        return _build_result(
            reference_text=normalized_reference_text,
            identifier_type="doi",
            identifier_value=doi,
            download_status="abstract_only",
            pdf_path="",
            abstract_excerpt=abstract_excerpt,
        )

    return _build_result(
        reference_text=normalized_reference_text,
        identifier_type="doi",
        identifier_value=doi,
        download_status="doi_found_but_failed",
        pdf_path="",
        abstract_excerpt="",
    )


def resolve_reference_metadata(reference_text: str) -> dict[str, str]:
    normalized_reference_text = str(reference_text or "").strip()
    if not normalized_reference_text:
        return {
            "identifier_type": "",
            "identifier_value": "",
            "identifier_source": "",
            "lookup_status": "no_reference_text",
            "abstract_excerpt": "",
        }

    doi = extract_doi(normalized_reference_text)
    if doi:
        return _metadata_result(
            identifier_type="doi",
            identifier_value=doi,
            identifier_source="reference_text",
            lookup_status="doi_in_reference",
            abstract_excerpt=fetch_reference_abstract(
                doi, normalized_reference_text
            ).get("abstract_excerpt", ""),
        )

    metadata = _lookup_reference_metadata(normalized_reference_text)
    if metadata.get("identifier_value"):
        doi = metadata["identifier_value"]
        return _metadata_result(
            identifier_type="doi",
            identifier_value=doi,
            identifier_source=metadata["identifier_source"],
            lookup_status=metadata["lookup_status"],
            abstract_excerpt=metadata.get("abstract_excerpt", ""),
        )

    return _metadata_result(
        identifier_type="",
        identifier_value="",
        identifier_source="",
        lookup_status="no_identifier",
        abstract_excerpt="",
    )


def fetch_reference_abstract(doi: str, reference_text: str = "") -> dict[str, str]:
    normalized_doi = _normalize_doi(doi)
    if not normalized_doi:
        return {
            "abstract_excerpt": "",
            "abstract_source": "",
            "abstract_status": "no_identifier",
        }

    openalex_abstract = _extract_abstract_excerpt(_fetch_openalex_work(normalized_doi))
    if openalex_abstract and not _looks_like_page_chrome(openalex_abstract):
        return {
            "abstract_excerpt": openalex_abstract,
            "abstract_source": "openalex",
            "abstract_status": "found",
        }

    semantic_metadata = _lookup_semantic_scholar_by_doi(normalized_doi)
    semantic_abstract = str(semantic_metadata.get("abstract_excerpt", "")).strip()
    if semantic_abstract:
        return {
            "abstract_excerpt": semantic_abstract,
            "abstract_source": "semantic_scholar",
            "abstract_status": "found",
        }

    europepmc_metadata = _lookup_europepmc_by_doi(normalized_doi)
    europepmc_abstract = str(europepmc_metadata.get("abstract_excerpt", "")).strip()
    if europepmc_abstract:
        return {
            "abstract_excerpt": europepmc_abstract,
            "abstract_source": "europepmc",
            "abstract_status": "found",
        }

    crossref_abstract = _lookup_crossref_abstract_by_doi(normalized_doi)
    if crossref_abstract:
        return {
            "abstract_excerpt": crossref_abstract,
            "abstract_source": "crossref",
            "abstract_status": "found",
        }

    if reference_text:
        searched_metadata = _lookup_reference_metadata(reference_text)
        searched_abstract = str(searched_metadata.get("abstract_excerpt", "")).strip()
        if (
            searched_abstract
            and _normalize_doi(searched_metadata.get("identifier_value"))
            == normalized_doi
        ):
            return {
                "abstract_excerpt": searched_abstract,
                "abstract_source": str(
                    searched_metadata.get("identifier_source", "") or "reference_search"
                ),
                "abstract_status": "found",
            }

    return {
        "abstract_excerpt": "",
        "abstract_source": "",
        "abstract_status": "not_found",
    }


def _looks_like_page_chrome(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    if not normalized:
        return False
    if normalized.startswith(("advertisement return to issue", "return to issue")):
        return True
    markers = (
        "return to issueprev",
        "view author information",
        "cite this:",
        "publication date",
        "publication history",
        "article views",
        "altmetric",
    )
    marker_count = sum(1 for marker in markers if marker in normalized)
    has_abstract_label = (
        normalized.startswith("abstract ") or " abstract " in normalized[:160]
    )
    return marker_count >= 2 and not has_abstract_label


def _lookup_reference_metadata(reference_text: str) -> dict[str, str]:
    crossref_doi = _lookup_crossref_doi(reference_text)
    if crossref_doi:
        return _metadata_result(
            identifier_type="doi",
            identifier_value=crossref_doi,
            identifier_source="crossref",
            lookup_status="doi_from_crossref",
            abstract_excerpt="",
        )

    openalex_doi = _lookup_openalex_doi(reference_text)
    if openalex_doi:
        return _metadata_result(
            identifier_type="doi",
            identifier_value=openalex_doi,
            identifier_source="openalex",
            lookup_status="doi_from_openalex",
            abstract_excerpt="",
        )

    for source_name, lookup_status, lookup_func in (
        (
            "semantic_scholar",
            "doi_from_semantic_scholar",
            _lookup_semantic_scholar_metadata,
        ),
        ("europepmc", "doi_from_europepmc", _lookup_europepmc_metadata),
        ("datacite", "doi_from_datacite", _lookup_datacite_metadata),
    ):
        metadata = lookup_func(reference_text)
        doi = str(metadata.get("identifier_value", "")).strip()
        if doi:
            return {
                "identifier_type": "doi",
                "identifier_value": doi,
                "identifier_source": source_name,
                "lookup_status": lookup_status,
                "abstract_excerpt": str(metadata.get("abstract_excerpt", "")).strip(),
                "pdf_url": str(metadata.get("pdf_url", "")).strip(),
            }

    return _metadata_result(
        identifier_type="",
        identifier_value="",
        identifier_source="",
        lookup_status="no_identifier",
        abstract_excerpt="",
    )


def _metadata_result(
    *,
    identifier_type: str,
    identifier_value: str,
    identifier_source: str,
    lookup_status: str,
    abstract_excerpt: str,
) -> dict[str, str]:
    return {
        "identifier_type": identifier_type,
        "identifier_value": identifier_value,
        "identifier_source": identifier_source,
        "lookup_status": lookup_status,
        "abstract_excerpt": abstract_excerpt,
    }


def _build_result(
    *,
    reference_text: str,
    identifier_type: str,
    identifier_value: str,
    download_status: str,
    pdf_path: str,
    abstract_excerpt: str,
) -> dict[str, str]:
    return {
        "reference_text": reference_text,
        "identifier_type": identifier_type,
        "identifier_value": identifier_value,
        "download_status": download_status,
        "pdf_path": pdf_path,
        "abstract_excerpt": abstract_excerpt,
    }


def _request_json(url: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
    for attempt in range(3):
        try:
            response = requests.get(
                url, headers={"User-Agent": _USER_AGENT}, params=params, timeout=30
            )
            response.raise_for_status()
            break
        except requests.RequestException:
            if attempt == 2:
                return {}
            time.sleep(0.5 * (attempt + 1))
    else:
        return {}

    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _lookup_crossref_doi(reference_text: str) -> str:
    payload = _request_json(
        _CROSSREF_WORKS_URL,
        params={
            "query.bibliographic": reference_text,
            "rows": str(_CROSSREF_ROWS),
        },
    )
    message = payload.get("message")
    if not isinstance(message, dict):
        return ""
    items = message.get("items")
    if not isinstance(items, list) or not items:
        return ""

    best_match: tuple[float, float, str] | None = None
    for item in items:
        if not isinstance(item, dict):
            continue
        match_score = _crossref_item_match_score(reference_text, item)
        if match_score is None:
            continue
        doi = str(item.get("DOI", "")).strip()
        if not doi:
            continue
        score, crossref_score = match_score
        candidate = (score, crossref_score, doi)
        if best_match is None or candidate > best_match:
            best_match = candidate

    return best_match[2] if best_match is not None else ""


def _lookup_openalex_doi(reference_text: str) -> str:
    payload = _request_json(
        _OPENALEX_SEARCH_URL,
        params={
            "search": reference_text,
            "per-page": "5",
        },
    )
    results = payload.get("results")
    if not isinstance(results, list):
        return ""

    best_match: tuple[float, str] | None = None
    for item in results:
        if not isinstance(item, dict):
            continue
        doi = _normalize_openalex_doi(item.get("doi"))
        if not doi:
            continue
        score = _openalex_item_match_score(reference_text, item)
        if score is None:
            continue
        candidate = (score, doi)
        if best_match is None or candidate > best_match:
            best_match = candidate
    return best_match[1] if best_match is not None else ""


def _lookup_semantic_scholar_metadata(reference_text: str) -> dict[str, str]:
    payload = _request_json(
        _SEMANTIC_SCHOLAR_SEARCH_URL,
        params={
            "query": reference_text,
            "limit": str(_FALLBACK_ROWS),
            "fields": "title,year,authors,externalIds,abstract,openAccessPdf,venue",
        },
    )
    data = payload.get("data")
    if not isinstance(data, list):
        return _empty_lookup_metadata()

    best_match: tuple[float, dict[str, Any]] | None = None
    for item in data:
        if not isinstance(item, dict):
            continue
        doi = _extract_semantic_scholar_doi(item)
        if not doi:
            continue
        score = _generic_item_match_score(
            reference_text,
            title=str(item.get("title", "") or ""),
            year=str(item.get("year", "") or ""),
            authors=_semantic_scholar_author_names(item),
            venue=str(item.get("venue", "") or ""),
        )
        if score is None:
            continue
        candidate = (score, item)
        if best_match is None or candidate[0] > best_match[0]:
            best_match = candidate

    if best_match is None:
        return _empty_lookup_metadata()
    item = best_match[1]
    return {
        "identifier_type": "doi",
        "identifier_value": _extract_semantic_scholar_doi(item),
        "abstract_excerpt": _trim_abstract(str(item.get("abstract", "") or "")),
        "pdf_url": _extract_semantic_scholar_pdf_url(item),
    }


def _lookup_europepmc_metadata(reference_text: str) -> dict[str, str]:
    payload = _request_json(
        _EUROPEPMC_SEARCH_URL,
        params={
            "query": reference_text,
            "format": "json",
            "pageSize": str(_FALLBACK_ROWS),
            "resultType": "core",
        },
    )
    result_list = payload.get("resultList")
    results = result_list.get("result") if isinstance(result_list, dict) else None
    if not isinstance(results, list):
        return _empty_lookup_metadata()

    best_match: tuple[float, dict[str, Any]] | None = None
    for item in results:
        if not isinstance(item, dict):
            continue
        doi = str(item.get("doi", "") or "").strip()
        if not doi:
            continue
        score = _generic_item_match_score(
            reference_text,
            title=str(item.get("title", "") or ""),
            year=str(item.get("pubYear", "") or ""),
            authors=_split_author_string(str(item.get("authorString", "") or "")),
            venue=str(item.get("journalTitle", "") or ""),
        )
        if score is None:
            continue
        candidate = (score, item)
        if best_match is None or candidate[0] > best_match[0]:
            best_match = candidate

    if best_match is None:
        return _empty_lookup_metadata()
    item = best_match[1]
    return {
        "identifier_type": "doi",
        "identifier_value": str(item.get("doi", "") or "").strip(),
        "abstract_excerpt": _trim_abstract(str(item.get("abstractText", "") or "")),
        "pdf_url": _extract_europepmc_pdf_url(item),
    }


def _lookup_datacite_metadata(reference_text: str) -> dict[str, str]:
    payload = _request_json(
        _DATACITE_DOIS_URL,
        params={
            "query": reference_text,
            "page[size]": str(_FALLBACK_ROWS),
        },
    )
    data = payload.get("data")
    if not isinstance(data, list):
        return _empty_lookup_metadata()

    best_match: tuple[float, dict[str, Any]] | None = None
    for item in data:
        if not isinstance(item, dict):
            continue
        doi = str(item.get("id", "") or "").strip()
        attributes = item.get("attributes")
        if not doi or not isinstance(attributes, dict):
            continue
        score = _generic_item_match_score(
            reference_text,
            title=_extract_datacite_title(attributes),
            year=str(attributes.get("publicationYear", "") or ""),
            authors=_extract_datacite_creator_names(attributes),
            venue=str(attributes.get("publisher", "") or ""),
        )
        if score is None:
            continue
        candidate = (score, item)
        if best_match is None or candidate[0] > best_match[0]:
            best_match = candidate

    if best_match is None:
        return _empty_lookup_metadata()
    item = best_match[1]
    return {
        "identifier_type": "doi",
        "identifier_value": str(item.get("id", "") or "").strip(),
        "abstract_excerpt": "",
        "pdf_url": "",
    }


def _normalize_openalex_doi(value: object) -> str:
    doi = str(value or "").strip()
    if not doi:
        return ""
    doi = re.sub(r"(?i)^https?://(?:dx\.)?doi\.org/", "", doi).strip()
    return doi


def _openalex_item_match_score(
    reference_text: str, item: dict[str, Any]
) -> float | None:
    reference_tokens = {
        token
        for token in _tokenize_text(reference_text)
        if len(token) >= 4 and token not in _STOPWORDS
    }
    if not reference_tokens:
        return None

    score = 0.0
    corroborating_signals = 0
    reference_years = _extract_year_tokens(reference_text)
    publication_year = str(item.get("publication_year", "") or "").strip()
    if reference_years and publication_year:
        if publication_year not in reference_years:
            return None
        score += 2.0
        corroborating_signals += 1

    title = str(item.get("title", "") or "")
    title_tokens = {
        token
        for token in _tokenize_text(title)
        if len(token) >= 4 and token not in _STOPWORDS
    }
    title_overlap = reference_tokens.intersection(title_tokens)
    if title_tokens and title_overlap:
        score += min(4.0, float(len(title_overlap)))
        corroborating_signals += 1

    authorships = item.get("authorships")
    author_tokens: set[str] = set()
    if isinstance(authorships, list):
        for authorship in authorships:
            if not isinstance(authorship, dict):
                continue
            author = authorship.get("author")
            if isinstance(author, dict):
                author_tokens.update(
                    _tokenize_text(str(author.get("display_name", "") or ""))
                )
    author_overlap = reference_tokens.intersection(author_tokens)
    if author_overlap:
        score += min(3.0, 1.0 + 0.5 * len(author_overlap))
        corroborating_signals += 1

    primary_location = item.get("primary_location")
    source = (
        primary_location.get("source") if isinstance(primary_location, dict) else None
    )
    source_name = (
        str(source.get("display_name", "") or "") if isinstance(source, dict) else ""
    )
    source_tokens = {
        token
        for token in _tokenize_text(source_name)
        if len(token) >= 3 and token not in _STOPWORDS
    }
    if reference_tokens.intersection(source_tokens):
        score += 1.5
        corroborating_signals += 1

    return score if corroborating_signals >= 2 else None


def _fetch_openalex_work(doi: str) -> dict[str, Any]:
    return _request_json(_OPENALEX_WORKS_URL.format(doi=quote(doi, safe="")))


def _lookup_semantic_scholar_by_doi(doi: str) -> dict[str, str]:
    normalized_doi = _normalize_doi(doi)
    if not normalized_doi:
        return _empty_lookup_metadata()
    payload = _request_json(
        _SEMANTIC_SCHOLAR_PAPER_URL.format(doi=quote(normalized_doi, safe="")),
        params={
            "fields": "title,year,authors,externalIds,abstract,openAccessPdf,venue",
        },
    )
    if not payload:
        return _empty_lookup_metadata()
    return {
        "identifier_type": "doi",
        "identifier_value": _extract_semantic_scholar_doi(payload) or normalized_doi,
        "abstract_excerpt": _trim_abstract(str(payload.get("abstract", "") or "")),
        "pdf_url": _extract_semantic_scholar_pdf_url(payload),
    }


def _lookup_europepmc_by_doi(doi: str) -> dict[str, str]:
    normalized_doi = _normalize_doi(doi)
    if not normalized_doi:
        return _empty_lookup_metadata()
    payload = _request_json(
        _EUROPEPMC_SEARCH_URL,
        params={
            "query": f"DOI:{normalized_doi}",
            "format": "json",
            "pageSize": "1",
            "resultType": "core",
        },
    )
    result_list = payload.get("resultList")
    results = result_list.get("result") if isinstance(result_list, dict) else None
    if not isinstance(results, list) or not results:
        return _empty_lookup_metadata()
    item = results[0] if isinstance(results[0], dict) else {}
    return {
        "identifier_type": "doi",
        "identifier_value": str(item.get("doi", "") or normalized_doi).strip(),
        "abstract_excerpt": _trim_abstract(str(item.get("abstractText", "") or "")),
        "pdf_url": _extract_europepmc_pdf_url(item),
    }


def _lookup_crossref_abstract_by_doi(doi: str) -> str:
    normalized_doi = _normalize_doi(doi)
    if not normalized_doi:
        return ""
    payload = _request_json(
        _CROSSREF_WORK_BY_DOI_URL.format(doi=quote(normalized_doi, safe=""))
    )
    message = payload.get("message")
    if not isinstance(message, dict):
        return ""
    return _trim_abstract(_strip_markup(str(message.get("abstract", "") or "")))


def _empty_lookup_metadata() -> dict[str, str]:
    return {
        "identifier_type": "",
        "identifier_value": "",
        "abstract_excerpt": "",
        "pdf_url": "",
    }


def _extract_semantic_scholar_doi(item: dict[str, Any]) -> str:
    external_ids = item.get("externalIds")
    if not isinstance(external_ids, dict):
        return ""
    return _normalize_doi(external_ids.get("DOI"))


def _extract_semantic_scholar_pdf_url(item: dict[str, Any]) -> str:
    open_access_pdf = item.get("openAccessPdf")
    if not isinstance(open_access_pdf, dict):
        return ""
    return str(open_access_pdf.get("url", "") or "").strip()


def _semantic_scholar_author_names(item: dict[str, Any]) -> list[str]:
    authors = item.get("authors")
    if not isinstance(authors, list):
        return []
    names: list[str] = []
    for author in authors:
        if not isinstance(author, dict):
            continue
        name = str(author.get("name", "") or "").strip()
        if name:
            names.append(name)
    return names


def _extract_europepmc_pdf_url(item: dict[str, Any]) -> str:
    full_text_url_list = item.get("fullTextUrlList")
    full_text_urls = (
        full_text_url_list.get("fullTextUrl")
        if isinstance(full_text_url_list, dict)
        else None
    )
    if not isinstance(full_text_urls, list):
        return ""
    for full_text_url in full_text_urls:
        if not isinstance(full_text_url, dict):
            continue
        document_style = str(full_text_url.get("documentStyle", "") or "").lower()
        availability = str(full_text_url.get("availability", "") or "").lower()
        url = str(full_text_url.get("url", "") or "").strip()
        if (
            url
            and ("pdf" in document_style or url.lower().endswith(".pdf"))
            and "subscription" not in availability
        ):
            return url
    return ""


def _extract_datacite_title(attributes: dict[str, Any]) -> str:
    titles = attributes.get("titles")
    if not isinstance(titles, list):
        return ""
    for item in titles:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "") or "").strip()
        if title:
            return title
    return ""


def _extract_datacite_creator_names(attributes: dict[str, Any]) -> list[str]:
    creators = attributes.get("creators")
    if not isinstance(creators, list):
        return []
    names: list[str] = []
    for creator in creators:
        if not isinstance(creator, dict):
            continue
        name = str(creator.get("name", "") or "").strip()
        if name:
            names.append(name)
    return names


def _split_author_string(value: str) -> list[str]:
    return [part.strip() for part in re.split(r";|\band\b|,", value) if part.strip()]


def _trim_abstract(value: str) -> str:
    return " ".join(str(value or "").split())[:_ABSTRACT_EXCERPT_LIMIT].strip()


def _strip_markup(value: str) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split()).strip()


def _normalize_doi(value: object) -> str:
    doi = str(value or "").strip()
    if not doi:
        return ""
    return re.sub(r"(?i)^https?://(?:dx\.)?doi\.org/", "", doi).strip()


def _generic_item_match_score(
    reference_text: str,
    *,
    title: str,
    year: str,
    authors: list[str],
    venue: str,
) -> float | None:
    reference_tokens = {
        token
        for token in _tokenize_text(reference_text)
        if len(token) >= 4 and token not in _STOPWORDS
    }
    if not reference_tokens:
        return None

    score = 0.0
    corroborating_signals = 0
    reference_years = _extract_year_tokens(reference_text)
    if reference_years and str(year).strip():
        if str(year).strip() not in reference_years:
            return None
        score += 2.0
        corroborating_signals += 1

    title_tokens = {
        token
        for token in _tokenize_text(title)
        if len(token) >= 4 and token not in _STOPWORDS
    }
    title_overlap = reference_tokens.intersection(title_tokens)
    if title_tokens and title_overlap:
        score += min(4.0, float(len(title_overlap)))
        corroborating_signals += 1

    author_tokens: set[str] = set()
    for author in authors:
        author_tokens.update(_tokenize_text(author))
    author_overlap = reference_tokens.intersection(author_tokens)
    if author_overlap:
        score += min(3.0, 1.0 + 0.5 * len(author_overlap))
        corroborating_signals += 1

    venue_tokens = {
        token
        for token in _tokenize_text(venue)
        if len(token) >= 3 and token not in _STOPWORDS
    }
    if venue_tokens and reference_tokens.intersection(venue_tokens):
        score += 1.5
        corroborating_signals += 1

    return score if corroborating_signals >= 2 else None


def _crossref_item_match_score(
    reference_text: str, item: dict[str, Any]
) -> tuple[float, float] | None:
    reference_tokens = set(_tokenize_text(reference_text))
    if not reference_tokens:
        return None

    raw_title = " ".join(
        str(title) for title in item.get("title", []) if isinstance(title, str)
    )
    if _CROSSREF_NON_ARTICLE_TITLE_RE.search(raw_title):
        return None

    score = 0.0
    corroborating_signals = 0
    reference_years = _extract_year_tokens(reference_text)

    candidate_year = _extract_crossref_year(item)
    if candidate_year and reference_years:
        if str(candidate_year) not in reference_years:
            return None
        corroborating_signals += 1
        score += 3.0

    author_families = _extract_crossref_author_families(item)
    author_overlap: set[str] = set()
    if author_families:
        author_overlap = reference_tokens.intersection(author_families)
        if not author_overlap:
            return None
        corroborating_signals += 1
        score += min(3.0, 1.5 + 0.5 * len(author_overlap))

    title_tokens = _extract_crossref_title_tokens(item)
    title_overlap = len(reference_tokens.intersection(title_tokens))
    if title_tokens:
        if title_overlap == 0:
            if corroborating_signals == 0:
                return None
        else:
            title_overlap_ratio = title_overlap / max(1, len(title_tokens))
            score += min(6.0, float(title_overlap))
            if title_overlap_ratio >= 0.55:
                score += 4.0
            corroborating_signals += 1

    journal_tokens = _extract_crossref_container_title_tokens(item)
    journal_overlap = reference_tokens.intersection(journal_tokens)
    if journal_tokens:
        if not journal_overlap and corroborating_signals == 0:
            return None
        if journal_overlap:
            corroborating_signals += 1
            score += 2.0

    volume_tokens = _extract_crossref_volume_tokens(item)
    page_tokens = _extract_crossref_page_tokens(item)
    has_page_overlap = False
    if reference_tokens.intersection(page_tokens):
        has_page_overlap = True
        corroborating_signals += 1
        score += 5.0
    elif reference_tokens.intersection(volume_tokens):
        corroborating_signals += 1
        score += 1.5

    if _is_short_structured_reference(reference_text):
        # Short references often contain only authors, abbreviated venue, year,
        # volume, and pages. Crossref can rank unrelated records highly when a
        # year/page coincides, so require the identity-bearing signals.
        if not author_overlap:
            return None
        if page_tokens and not has_page_overlap:
            return None
        if (
            title_tokens
            and title_overlap == 0
            and not journal_overlap
            and not has_page_overlap
        ):
            return None

    if corroborating_signals < 2:
        return None
    return score, float(item.get("score", 0.0) or 0.0)


def _is_short_structured_reference(reference_text: str) -> bool:
    text = str(reference_text or "")
    tokens = _tokenize_text(text)
    if len(tokens) <= 18:
        return True
    title_markers = (
        ":",
        "?",
        "highly",
        "efficient",
        "photochemical",
        "photocatalytic",
        "reduction",
    )
    return len(tokens) <= 28 and not any(
        marker in text.lower() for marker in title_markers
    )


def _tokenize_text(text: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []

    normalized_text = unicodedata.normalize("NFC", text).lower()

    for char in normalized_text:
        if char.isalnum():
            current.append(char)
            continue
        if current:
            tokens.append("".join(current))
            current.clear()

    if current:
        tokens.append("".join(current))

    return tokens


def _extract_crossref_year(item: dict[str, Any]) -> int | None:
    for field_name in ("published-print", "published-online", "issued"):
        field_value = item.get(field_name)
        if not isinstance(field_value, dict):
            continue
        date_parts = field_value.get("date-parts")
        if not isinstance(date_parts, list) or not date_parts:
            continue
        first_part = date_parts[0]
        if (
            isinstance(first_part, list)
            and first_part
            and isinstance(first_part[0], int)
        ):
            return first_part[0]
    return None


def _extract_crossref_author_families(item: dict[str, Any]) -> set[str]:
    authors = item.get("author")
    if not isinstance(authors, list):
        return set()
    families: set[str] = set()
    for author in authors:
        if not isinstance(author, dict):
            continue
        family = author.get("family")
        if isinstance(family, str):
            families.update(_tokenize_text(family))
    return families


def _extract_crossref_title_tokens(item: dict[str, Any]) -> set[str]:
    titles = item.get("title")
    if not isinstance(titles, list):
        return set()
    title_tokens: set[str] = set()
    for title in titles:
        if not isinstance(title, str):
            continue
        for token in _tokenize_text(title):
            if len(token) >= 4 and token not in _STOPWORDS:
                title_tokens.add(token)
    return title_tokens


def _extract_crossref_container_title_tokens(item: dict[str, Any]) -> set[str]:
    container_titles = item.get("container-title")
    if not isinstance(container_titles, list):
        return set()
    container_tokens: set[str] = set()
    for container_title in container_titles:
        if not isinstance(container_title, str):
            continue
        for token in _tokenize_text(container_title):
            if len(token) >= 3 and token not in _STOPWORDS:
                container_tokens.add(token)
    return container_tokens


def _extract_crossref_volume_tokens(item: dict[str, Any]) -> set[str]:
    volume = str(item.get("volume", "") or "").strip()
    return set(_PAGE_TOKEN_RE.findall(volume))


def _extract_crossref_page_tokens(item: dict[str, Any]) -> set[str]:
    page_value = str(item.get("page", "") or "").strip()
    if not page_value:
        return set()
    tokens: set[str] = set()
    for chunk in re.split(r"[-–—,;/\s]+", page_value):
        if chunk.isdigit():
            tokens.add(chunk)
    return tokens


def _extract_year_tokens(text: str) -> set[str]:
    return {
        match.group(0) for match in re.finditer(r"\b(?:19|20)\d{2}\b", str(text or ""))
    }


def _extract_abstract_excerpt(work: dict[str, Any]) -> str:
    abstract_index = work.get("abstract_inverted_index")
    if not isinstance(abstract_index, dict):
        return ""
    abstract_text = _reconstruct_abstract(abstract_index)
    if not abstract_text:
        return ""
    compact_text = " ".join(abstract_text.split())
    return compact_text[:_ABSTRACT_EXCERPT_LIMIT].strip()


def _reconstruct_abstract(abstract_index: dict[str, Any]) -> str:
    indexed_words: list[tuple[int, str]] = []
    for word, positions in abstract_index.items():
        if not isinstance(word, str) or not isinstance(positions, list):
            continue
        for position in positions:
            if isinstance(position, int):
                indexed_words.append((position, word))
    indexed_words.sort(key=lambda item: item[0])
    return " ".join(word for _, word in indexed_words)
