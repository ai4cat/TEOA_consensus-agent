from __future__ import annotations
from dataclasses import asdict,dataclass

@dataclass(slots=True)
class ChunkRecord:
    doc_id: str
    filename: str
    page_number: int
    paragraph_id: str
    chunk_id: str
    char_start: int
    char_end: int
    text: str
    clean_text: str = ""
    section_title: str = ""
    sentence_start: str = ""
    sentence_end: str = ""
    token_estimate: int = 0
    doc_type: str = ""
    chunk_type: str = ""
    source_block_type: str = ""
    source_block_id: str = ""
    source_bbox_json: str = ""
    source_heading_level: str = ""

    def to_manifest_row(self) -> dict[str, object]:
        return asdict(self)

