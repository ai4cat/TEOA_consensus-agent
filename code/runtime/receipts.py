"""Validate the file references in candidate-preparation receipts."""

from pathlib import Path
import json





def validate_receipt(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    for group in (value["contract"]["inputs"], value["outputs"]):
        for path in group:
            if not Path(path).is_file():
                raise ValueError("Stage input/output changed: " + path)
    return value
