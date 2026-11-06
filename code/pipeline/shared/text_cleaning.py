"""Mechanical text normalization and bibliographic layout recognition."""

import re


def clean_text_for_model(text: str) -> str:
    return " ".join(str(text or "").split())


def split_sentences_for_evidence(text: str) -> list[str]:
    normalized = clean_text_for_model(text)
    return [
        s.strip()
        for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(])", normalized)
        if s.strip()
    ]


def looks_like_reference_entry(text: str) -> bool:
    """Recognize numbered bibliography layout, without scientific title terms."""
    value = clean_text_for_model(text)
    numbered = re.match(r"^(?:\[\d{1,3}\]|\d{1,3}[.)])\s+", value)
    author = re.search(r"\b[A-Z][a-zA-Z-]+,\s*[A-Z]\.", value)
    year = re.search(r"\b(?:19|20)\d{2}\b", value)
    return bool(numbered and author and year)
