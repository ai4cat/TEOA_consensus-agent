"""Read authoritative DOI metadata from the main input catalog."""

import csv
import hashlib
import re
from functools import lru_cache
from pathlib import Path

from preparation.parsing.source_metadata import extract_source_metadata


METADATA_FIELDS = (
    "source_id", "relative_path", "discovery_doc_ids", "doi", "doi_source",
    "sha256", "metadata_provenance", "pdf_identity_revalidated_by_this_change",
)


@lru_cache(maxsize=4)
def _load_catalog(path, modified_ns, size):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not set(METADATA_FIELDS).issubset(reader.fieldnames or []):
            raise ValueError("Main catalog is missing authoritative metadata fields")
        records = {}
        for row in reader:
            source_id = row["source_id"]
            if not source_id or source_id in records:
                raise ValueError("Missing or duplicate source identity in main catalog")
            if not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]):
                raise ValueError("Invalid source SHA256 in main catalog: " + source_id)
            verified = row["pdf_identity_revalidated_by_this_change"].lower()
            if verified not in ("true", "false"):
                raise ValueError("Invalid PDF identity verification flag: " + source_id)
            records[source_id] = {
                "sha256": row["sha256"],
                "doi": row["doi"],
                "doc_id": row["discovery_doc_ids"],
                "doi_source": row["doi_source"],
                "provenance": row["metadata_provenance"],
                "pdf_identity_revalidated_by_this_change": verified == "true",
            }
    return records


def source_metadata(root, path, payload):
    metadata = extract_source_metadata(payload)
    catalog_path = Path(root) / "inputs/02_documents/catalog.csv"
    stat = catalog_path.stat()
    records = _load_catalog(
        str(catalog_path.resolve()), stat.st_mtime_ns, stat.st_size
    )
    correction = records.get(Path(path).stem)
    if correction is None:
        if Path(path).resolve().is_relative_to(catalog_path.parent.resolve()):
            raise ValueError("Source is absent from main catalog: " + Path(path).name)
        return metadata
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if digest != correction["sha256"]:
        raise ValueError("Reviewed metadata source has changed: " + Path(path).name)
    metadata["original_extraction"] = {
        key: metadata.get(key) for key in ("doi", "title", "doi_source", "title_source")
    }
    metadata["doi"] = correction["doi"]
    metadata["doi_source"] = correction["doi_source"]
    if correction["doi"]:
        metadata["flags"] = [flag for flag in metadata["flags"] if flag != "unresolved_doi"]
    metadata["curation"] = correction
    metadata["flags"].append("reviewed_metadata_correction_applied")
    return metadata
