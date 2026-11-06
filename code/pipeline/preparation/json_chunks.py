from __future__ import annotations
import json
from pathlib import Path
from shared.schemas import ChunkRecord
from shared.settings import PipelineConfig
from shared.text_cleaning import clean_text_for_model
from preparation.parsing.mineru_json_adapter import load_mineru_json_entries


def _build_mineru_json_chunks_for_row(
    *,
    row: dict[str, str],
    config: PipelineConfig,
) -> list[ChunkRecord]:
    _ = config
    json_path = Path(str(row.get("pdf_path", "")).strip())
    if not json_path.exists():
        raise FileNotFoundError(
            f"MinerU JSON-backed Step 2 expected JSON source to exist: {json_path}"
        )

    entries = load_mineru_json_entries(json_path)
    chunks: list[ChunkRecord] = []
    page_cursors: dict[int, int] = {}
    page_paragraph_counts: dict[int, int] = {}
    for entry in entries:
        page_number = entry.page_number
        paragraph_index = page_paragraph_counts.get(page_number, 0) + 1
        char_start = page_cursors.get(page_number, 0)
        char_end = char_start + len(entry.text)
        chunk = _build_chunk_record(
            row=row,
            page_number=page_number,
            paragraph_index=paragraph_index,
            text=entry.text,
            char_start=char_start,
            char_end=char_end,
            section_title=entry.section_title,
            chunk_type=entry.chunk_type,
            source_block_type=entry.source_block_type,
            source_block_id=entry.source_block_id,
            source_bbox_json=_encode_bboxes_json(
                [tuple(bbox) for bbox in entry.source_bboxes]
            ),
            source_heading_level=entry.source_heading_level,
        )
        if chunk is None:
            continue
        chunks.append(chunk)
        page_paragraph_counts[page_number] = paragraph_index
        page_cursors[page_number] = char_end + 2
    return chunks


def _encode_bboxes_json(bboxes: list[tuple[float, float, float, float]]) -> str:
    return json.dumps(
        [[float(value) for value in bbox] for bbox in bboxes],
        ensure_ascii=False,
    )


def _build_chunk_record(
    *,
    row: dict[str, str],
    page_number: int,
    paragraph_index: int,
    text: str,
    char_start: int,
    char_end: int,
    section_title: str = "",
    chunk_type: str = "",
    source_block_type: str = "",
    source_block_id: str = "",
    source_bbox_json: str = "",
    source_heading_level: str = "",
) -> ChunkRecord | None:
    clean_text = (
        text.strip() if chunk_type == "reference_entry" else clean_text_for_model(text)
    )
    if not clean_text:
        return None
    paragraph_id = f"p{paragraph_index:02d}"
    chunk_id = f"{row['doc_id']}_p{page_number}_{paragraph_id}_c0"
    return ChunkRecord(
        doc_id=row["doc_id"],
        filename=row["filename"],
        page_number=page_number,
        paragraph_id=paragraph_id,
        chunk_id=chunk_id,
        char_start=char_start,
        char_end=char_end,
        text=text,
        clean_text=clean_text,
        section_title=section_title,
        token_estimate=max(1, len(text.split())),
        doc_type=row.get("doc_type", ""),
        chunk_type=chunk_type,
        source_block_type=source_block_type,
        source_block_id=source_block_id,
        source_bbox_json=source_bbox_json,
        source_heading_level=source_heading_level,
    )
