"""Rebuild all local analysis inputs from source JSON, without model calls."""

import re
import multiprocessing
import signal
from collections import deque
from concurrent.futures import ProcessPoolExecutor, wait
from contextlib import closing
from runtime.artifacts import read, save, table
from preparation.parsing.mineru_json_adapter import parse_mineru_json_file
from runtime.curated_metadata import source_metadata
from preparation.parsing.content_list import normalize_content_list
from preparation.json_chunks import _build_mineru_json_chunks_for_row
from preparation.author_abstracts import (
    _extract_author_abstract_row,
    AUTHOR_ABSTRACT_FIELDS,
)
from shared.pdf_utils import extract_year, infer_journal, infer_doc_type


def text_piece(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(filter(None, (text_piece(x) for x in value)))
    if isinstance(value, dict):
        if "type" in value:
            return text_piece(value.get("content"))
        return " ".join(
            filter(
                None,
                (
                    text_piece(value[k])
                    for k in (
                        "title_content",
                        "paragraph_content",
                        "list_content",
                        "content",
                    )
                    if k in value
                ),
            )
        )
    return ""


def normalize_system_source(pages):
    if not isinstance(pages, list) or not all((isinstance(p, list) for p in pages)):
        raise ValueError("Systems source must be the original page-nested parsed JSON")
    normalized = []
    for page_index, page in enumerate(pages):
        for block_index, block in enumerate(page):
            if not isinstance(block, dict):
                raise ValueError("Invalid source block")
            kind = block.get("type", "")
            if kind not in ("paragraph", "title", "list", "text", "ref_text"):
                continue
            text = text_piece(block)
            if text.strip():
                normalized.append(
                    {
                        "type": "text",
                        "text": text,
                        "page_idx": page_index,
                        "bbox": block.get("bbox"),
                        "original_block_type": kind,
                        "original_page_block_index": block_index,
                    }
                )
    return normalized


def system_payloads(doc_id, blocks):
    parts = []
    for index, block in enumerate(blocks):
        text = block["text"]
        start = 0
        while start < len(text):
            end = min(start + 22000, len(text))
            parts.append(
                {
                    "block_id": f"b{index:05d}_{start}",
                    "text": text[start:end],
                    "flat_block_index": index,
                    "page": block["page_idx"] + 1,
                    "original_page_block_index": block["original_page_block_index"],
                    "original_type": block["original_block_type"],
                    "start": start,
                    "end": end,
                }
            )
            if end == len(text):
                break
            start = end - 300
    batches = []
    batch = []
    size = 0
    for part in parts:
        if batch and size + len(part["text"]) > 24000:
            batches.append(batch)
            batch = []
            size = 0
        batch.append(part)
        size += len(part["text"])
    if batch:
        batches.append(batch)
    for j, batch in enumerate(batches):
        context = [
            b for b in (batches[j - 1][-2:] if j else []) if len(b["text"]) <= 2500
        ]
        yield {
            "doc_id": doc_id,
            "part": j,
            "parts": len(batches),
            "blocks": batch,
            "preceding_context": context,
        }


def _process_document(root, source_folder, work, pending, run_directory, doc_id, source):
    """Process one independent document; only write that document's artifacts."""
    from runtime.control import Controller

    controller = Controller(run_directory)
    controller.check()
    checkpoint_root = work / "documents"
    path = source_folder / source["relative_path"]
    checkpoint = checkpoint_root / (doc_id + ".json")
    payload = read(path)
    parsed = parse_mineru_json_file(path)
    metadata = source_metadata(root, path, payload)
    controller.check()
    title = metadata.get("title", "")
    first = metadata.get("first_page_text", "")
    doc = {
        "doc_id": doc_id,
        "filename": doc_id + ".json",
        "inferred_title": metadata.get("title_raw", ""),
        "canonical_title": title,
        "doi": metadata.get("doi", ""),
        "title_source": metadata.get("title_source", ""),
        "extracted_title_raw": metadata.get("title_raw", ""),
        "year": extract_year(first) or "unknown",
        "journal": infer_journal(first),
        "doc_type": metadata.get("doc_type")
        or infer_doc_type(title, parsed.full_text),
        "pdf_present": "N",
        "fulltext_path": "fulltext/" + doc_id + ".txt",
        "source_json": "inputs/02_documents/" + path.name,
    }
    match = re.fullmatch("doc_(\\d+)", doc_id)
    source_doc_id = f"doc_{int(match[1]):03d}" if match else doc_id
    row = {**doc, "doc_id": source_doc_id, "pdf_path": str(path)}
    local_chunks = [
        x.to_manifest_row()
        for x in _build_mineru_json_chunks_for_row(row=row, config=None)
    ]
    if not local_chunks:
        raise ValueError("Source JSON contains no usable text chunks: " + path.name)
    for item in local_chunks:
        item.update(doc_id=doc_id, source_doc_id=source_doc_id)
    controller.check()
    abstract_dir = work / "author_abstract_inputs"
    abstract_dir.mkdir(exist_ok=True)
    abstract_view = normalize_content_list(payload)
    save(abstract_dir / (doc_id + ".json"), abstract_view)
    abstract = _extract_author_abstract_row(
        doc_id=doc_id, corpus_row=doc, mineru_dir=abstract_dir
    )
    abstract["abstract_source"] = doc["source_json"]
    value = {
        "document": doc,
        "fulltext": parsed.full_text,
        "chunks": local_chunks,
        "abstract": abstract,
    }
    save(checkpoint, value)
    controller.check()
    out = pending / doc["fulltext_path"]
    out.parent.mkdir(exist_ok=True)
    out.write_text(value["fulltext"], encoding="utf-8")
    return value["document"], value["chunks"], value["abstract"]


def _initialize_worker():
    from runtime.control import disable_network

    # The parent owns Ctrl+C and publishes the shared cooperative pause flag.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    # Windows spawn does not inherit the parent's Stage 1 network guard.
    disable_network()


def _document_results(ctx, sources, workers=4):
    """Bound in-flight work and yield in source order for identical artifacts."""
    if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
        raise ValueError("corpus_workers must be a positive integer")
    args = (ctx.root, ctx.source("02_documents"), ctx.work, ctx.pending, ctx.run)
    if workers == 1:
        for doc_id, source in sources:
            ctx.controller.check()
            yield _process_document(*args, doc_id, source)
        return
    ctx.controller.check()
    pool = ProcessPoolExecutor(
        max_workers=min(workers, len(sources)),
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_initialize_worker,
    )
    futures = deque()
    remaining = iter(sources)

    def submit_next():
        item = next(remaining, None)
        if item is not None:
            ctx.controller.check()
            futures.append(pool.submit(_process_document, *args, *item))
    try:
        for _ in range(min(workers * 2, len(sources))):
            submit_next()
        while futures:
            future = futures[0]
            while not wait((future,), timeout=0.2).done:
                ctx.controller.check()
            ctx.controller.check()
            result = future.result()
            futures.popleft()
            submit_next()
            yield result
    finally:
        for future in futures:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)


def run(ctx):
    from runtime.corpus_reuse import reuse_corpus

    if reuse_corpus(ctx):
        return
    from runtime.library import catalog, ids

    sources = [
        (doc_id, row)
        for row in catalog(ctx.root)
        for doc_id in sorted(ids(row["discovery_doc_ids"]))
    ]
    sources.sort(key=lambda item: item[0])
    if not sources:
        raise ValueError("No main-library source JSON")
    docs = []
    chunks = []
    abstracts = []
    source_records = []
    checkpoint_root = ctx.work / "documents"
    checkpoint_root.mkdir(exist_ok=True)
    with closing(
        _document_results(ctx, sources, ctx.config.get("corpus_workers", 4))
    ) as results:
        for index, result in enumerate(results, 1):
            ctx.controller.check()
            doc, local_chunks, abstract = result
            docs.append(doc)
            chunks.extend(local_chunks)
            abstracts.append(abstract)
            source_records.append({"doc_id": doc["doc_id"], "path": doc["source_json"]})
            ctx.status(state="corpus", completed=index, total=len(sources))
    if len({r["chunk_id"] for r in chunks}) != len(chunks):
        raise ValueError("Duplicate chunk IDs")
    table(ctx.pending / "documents.csv", docs)
    table(ctx.pending / "chunks.csv", chunks)
    body = [r for r in chunks if r.get("chunk_type") == "body_paragraph"]
    table(ctx.pending / "body_paragraphs.csv", body)
    table(ctx.pending / "document_abstracts.csv", abstracts, AUTHOR_ABSTRACT_FIELDS)
    ctx.emit("source_index.json", source_records)
    ctx.emit(
        "summary.json",
        {
            "main_documents": len(docs),
            "chunks": len(chunks),
            "body_paragraphs": len(body),
            "author_abstracts_found": sum(
                (bool(r["abstract_text"]) for r in abstracts)
            ),
            "model_calls": 0,
            "origin": "generated from the declared discovery scope in the unified document store",
            "systems_collection_created": False,
        },
    )
