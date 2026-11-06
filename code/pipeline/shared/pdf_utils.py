from __future__ import annotations
import re
from dataclasses import dataclass

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)


YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


JOURNAL_RE = re.compile(
    r"(?P<journal>[A-Z][A-Za-z0-9&.,'()/\- ]{4,120})\s(?:19|20)\d{2},\s*\d+",
)


_REVIEW_TITLE_PATTERNS = (
    re.compile(r"\breview\b", re.I),
    re.compile(r"\bperspective\b", re.I),
    re.compile(r"\boverview\b", re.I),
    re.compile(r"\bmini[\s-]?review\b", re.I),
    re.compile(r"\btutorial review\b", re.I),
)


_REVIEW_TEXT_PATTERNS = (
    re.compile(r"\bin this review\b", re.I),
    re.compile(r"\bthis review\b", re.I),
    re.compile(r"\bwe review\b", re.I),
    re.compile(r"\bhere we review\b", re.I),
    re.compile(r"\breview article\b", re.I),
    re.compile(r"\bcomprehensive review\b", re.I),
    re.compile(r"\bthis perspective\b", re.I),
    re.compile(r"\bthis overview\b", re.I),
    re.compile(r"\bthis minireview\b", re.I),
)


_TEXTBOOK_FILENAME_PATTERNS = (
    re.compile(r"\bbook\b", re.I),
    re.compile(r"\bchapter\b", re.I),
    re.compile(r"\btextbook\b", re.I),
    re.compile(r"\bmonograph\b", re.I),
    re.compile(r"\bprinciples\b", re.I),
    re.compile(r"\bintroduction\b", re.I),
)


@dataclass(slots=True)
class PageText:
    page_number: int
    text: str


@dataclass(slots=True)
class ParsedPdf:
    pages: list[PageText]
    metadata_title: str = ""

    @property
    def full_text(self) -> str:
        return "\n\n".join(
            page.text.strip() for page in self.pages if page.text.strip()
        )

    @property
    def first_page_text(self) -> str:
        return self.pages[0].text if self.pages else ""


def extract_doi(text: str) -> str:
    cleaned = _clean_text(text)
    for candidate in DOI_RE.findall(cleaned):
        doi = candidate.rstrip(").,;]")
        if doi.lower().startswith("10.") and _looks_complete_doi(doi):
            return doi
    return ""


def _looks_complete_doi(doi: str) -> bool:
    try:
        suffix = doi.split("/", maxsplit=1)[1]
    except IndexError:
        return False
    # OCR often truncates Elsevier-style DOI prefixes to values such as
    # "10.1016/j.cej"; those should fall through to Crossref lookup.
    if len(suffix) < 6 and not any(char.isdigit() for char in suffix):
        return False
    if re.fullmatch(r"j\.[a-z]+", suffix, flags=re.I):
        return False
    if (
        re.search(r"\(SICI?\d?\)", suffix, flags=re.I)
        and "<" not in suffix
        and "::" not in suffix
    ):
        return False
    return True


def extract_year(text: str) -> str:
    match = YEAR_RE.search(text)
    return match.group(0) if match else ""


def infer_journal(text: str) -> str:
    cleaned = _clean_text(text)
    for line in cleaned.splitlines()[:30]:
        line = line.strip()
        if not line:
            continue
        match = JOURNAL_RE.search(line.replace("Cite This:", "").strip())
        if match:
            return _normalize_title(match.group("journal"))
        lowered = line.lower()
        if any(
            token in lowered
            for token in [
                "journal",
                "chem.",
                "chemistry",
                "materials",
                "catal.",
                "energy",
            ]
        ):
            if len(line) <= 140:
                return _normalize_title(line)
    return ""


def infer_doc_type(title: str, text: str) -> str:
    normalized_title = _normalize_doc_type_text(title)
    normalized_text = _normalize_doc_type_text(text)
    if any(pattern.search(normalized_title) for pattern in _REVIEW_TITLE_PATTERNS):
        return "review"
    if any(pattern.search(normalized_text) for pattern in _REVIEW_TEXT_PATTERNS):
        return "review"
    return "research"


def _normalize_doc_type_text(value: str) -> str:
    return re.sub(r"[_\-]+", " ", value)


def _clean_text(text: str) -> str:
    return (
        text.replace("\x00", " ")
        .replace("\u2010", "-")
        .replace("\u2011", "-")
        .replace("\u2012", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u00ad", "")
    )


def _normalize_title(value: str) -> str:
    return " ".join(value.replace("|", " ").split()).strip(" -:;,.")

