"""Snapshot primary sources; Stage 9 acquires cited articles online."""

import os
import shutil
import uuid
from pathlib import Path
from runtime.artifacts import read, save, inventory, table, validate
from runtime.agent_contract import LIBRARY
from runtime.registry import folder


def prepare_reference_inputs(root, run):
    from runtime.library import catalog
    from runtime.curated_metadata import METADATA_FIELDS

    root, run = (Path(root), Path(run))
    origin = {
        "mode": "primary_sources",
        "directory": None,
    }
    upstream = {
        str(n): (Path("state") / folder(n) / "completed.json").as_posix() for n in range(1, 9)
    }
    destination = run / LIBRARY
    if destination.exists():
        value = read(destination / "manifest.json")
        if value["origin"] != origin or value["upstream_receipts"] != upstream:
            raise ValueError(
                "Reference inputs differ from this run's primary sources or completed stages"
            )
        validate(destination / "02_documents", value["files"])
        return value
    staging = run / "work/agent_library" / ("import_" + uuid.uuid4().hex)
    documents = staging / "02_documents"
    documents.mkdir(parents=True)
    selected = []
    for row in catalog(root):
        if row["discovery_doc_ids"]:
            target = documents / row["relative_path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / "inputs/02_documents" / row["relative_path"], target)
            selected.append({**row, "input_prefix": LIBRARY + "/02_documents"})
    fields = [*METADATA_FIELDS, "input_prefix"]
    table(
        documents / "catalog.csv",
        sorted(selected, key=lambda r: r["source_id"]),
        fields,
    )
    value = {
        "schema": "agent.primary_sources.v1",
        "origin": origin,
        "upstream_receipts": upstream,
        "files": inventory(documents),
        "source_records": len(selected),
        "document_scope": "primary_sources_only",
    }
    save(staging / "manifest.json", value)
    destination.parent.mkdir(parents=True, exist_ok=True)
    ready = staging / "ready"
    ready.mkdir()
    os.replace(documents, ready / "02_documents")
    shutil.copy2(staging / "manifest.json", ready / "manifest.json")
    os.replace(ready, destination)
    return value
