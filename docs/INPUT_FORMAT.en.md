# Input format

[Back to README](../README.md) · [简体中文](INPUT_FORMAT.md)

## Main corpus

Place parsed JSON documents in `inputs/02_documents`. The bundled example uses a flat MinerU-style block list:

```json
[
  {"type": "text", "text": "Document title", "text_level": 1, "page_idx": 0, "bbox": [0, 0, 500, 50]},
  {"type": "text", "text": "A paragraph from your source.", "page_idx": 0, "bbox": [0, 60, 500, 150]}
]
```

The parser also accepts some nested Content List V2 and `pdf_info` structures. Check compatibility before importing output from a different converter.

The `catalog.csv` in the same directory must contain these columns:

| Column | Requirement |
| --- | --- |
| `source_id` | Unique source ID matching the JSON filename without its extension |
| `relative_path` | JSON path relative to the corpus directory; must remain inside it |
| `discovery_doc_ids` | Semicolon-separated discovery IDs, such as `doc_0001`; IDs must not repeat across rows |
| `doi` | Verified DOI, or an empty value if unknown |
| `doi_source` | Source of the DOI metadata |
| `sha256` | Lowercase, 64-character SHA-256 hash of the JSON file's raw bytes |
| `metadata_provenance` | Metadata source and review notes |
| `pdf_identity_revalidated_by_this_change` | `true` or `false`, reflecting whether document identity was revalidated |

Register every JSON file, and ensure every catalog path exists. Discovery stages process only sources with a nonempty `discovery_doc_ids` value.

After editing a JSON file, update its catalog hash:

```powershell
(Get-FileHash .\inputs\02_documents\YOUR_SOURCE.json -Algorithm SHA256).Hash.ToLowerInvariant()
```

For research runs, remove the synthetic entries and keep the files and catalog consistent. Check each source against its intended paper before assigning a verified DOI. The catalog DOI overrides automatic extraction; a valid hash alone does not verify paper identity.

## Independent catalytic-system corpus

The catalog at `inputs/03_platform_documents/catalog.csv` requires at least `doc_id` and `relative_path`. Each `doc_id` must be unique and contain only letters, digits, underscores, or hyphens.

Documents in this directory use a normalized, flat list of blocks. Each block has the following form:

```json
{
  "type": "text",
  "text": "Paragraph from a system-analysis source.",
  "page_idx": 0,
  "original_page_block_index": 0,
  "original_block_type": "paragraph",
  "bbox": [0, 0, 500, 100]
}
```

This format differs from the nested structures accepted by the main corpus parser. Convert nested MinerU output before adding it to this collection.

## Research prompt

`inputs/01_prompts/prompt.txt` must match the `PROMPT` constant in `code/pipeline/preparation/entity_selection.py`. The current prompt targets additives in photocatalytic CO₂ reduction. Update both copies when changing the research question.

Model settings and API configuration are described in the [usage guide](USAGE.md#model-configuration). Keep credentials out of catalogs, workflow JSON, and stage-parameter files.
