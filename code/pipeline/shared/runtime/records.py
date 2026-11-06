"""Run snapshots, descriptive artifact inventories and stage completion records."""

import csv
import json
import os
import platform
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from uuid import uuid4
from shared.runtime.paths import io_path
from shared.runtime.paths import project_root as package_project_root

REPO = package_project_root()
MODES = ("current_clean", "saved_input_audit", "frozen_reproduction")


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical_value(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def safe_output(path):
    text = str(path)
    if os.name == "nt" and text.startswith("\\\\?\\"):
        text = "\\\\" + text[8:] if text.startswith("\\\\?\\UNC\\") else text[4:]
    path = text
    path = Path(path).resolve()
    protected = (Path.home() / "Nutstore").resolve()
    if path == protected or path.is_relative_to(protected):
        raise ValueError("Nutstore is read-only; use a local working copy")
    return path


def write_json(path, value):
    path = safe_output(path)
    io_path(path.parent).mkdir(parents=True, exist_ok=True)
    with io_path(path).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def csv_rows(path):
    csv.field_size_limit(20000000)
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        first = next((line for line in stream if not line.startswith("#")), "")
        from itertools import chain

        yield from csv.DictReader(chain([first], stream))


def write_csv(path, rows, fields=None):
    rows = list(rows)
    fields = fields or list(dict.fromkeys((k for row in rows for k in row)))
    path = safe_output(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream, fields, extrasaction="raise", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)


def environment():
    return {
        "python": platform.python_version(),
        "os": platform.platform(),
        "packages": dict(
            sorted(
                (
                    (d.metadata["Name"].lower(), d.version)
                    for d in metadata.distributions()
                    if d.metadata["Name"]
                )
            )
        ),
        "environment_presence": {k: bool(v) for k, v in sorted(os.environ.items())},
    }


def code_identity():
    from shared.runtime.identity import package_identity

    return {"commit": None, "dirty": None, **package_identity()}


def file_record(path, root, *, row_key=None):
    path = Path(path)
    result = {
        "path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "rows": None,
        "unique_keys": None,
        "null_keys": None,
        "row_key": row_key,
        "schema_version": "entity.artifact_record.v1",
        "statistics_status": "not_tabular",
        "artifact_schema": "unversioned_non_tabular",
    }
    if path.suffix.lower() == ".csv":
        rows = list(csv_rows(path))
        result["artifact_schema"] = "entity.csv.columns.v1:" + canonical_value(
            list(rows[0]) if rows else []
        )
    elif path.suffix.lower() == ".jsonl":
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        ]
        result["artifact_schema"] = "entity.jsonl.fields.v1:" + canonical_value(
            sorted({k for row in rows if isinstance(row, dict) for k in row})
        )
    else:
        if path.suffix.lower() == ".json":
            value = read_json(path)
            result["artifact_schema"] = (
                value.get("schema", "unversioned_json")
                if isinstance(value, dict)
                else "unversioned_json_array"
            )
        return result
    if rows and all((isinstance(row, dict) for row in rows)):
        if row_key is None:
            row_key = next(
                (
                    key
                    for key in (
                        "node_id",
                        "evidence_id",
                        "raw_claim_id",
                        "old_claim_id",
                        "candidate_id",
                        "span_id",
                        "theme_id",
                        "consensus_claim_id",
                        "hypothesis_id",
                        "experiment_id",
                        "proposition_id",
                        "doc_id",
                        "path",
                    )
                    if key in rows[0]
                ),
                None,
            )
        result["row_key"] = row_key
    result["rows"] = len(rows)
    result["statistics_status"] = (
        "computed" if row_key else "no_declared_identity_column"
    )
    if row_key:
        values = [r.get(row_key) for r in rows]
        encoded = [canonical_value(v) for v in values if v is not None and v != ""]
        result.update(
            unique_keys=len(set(encoded)), null_keys=len(values) - len(encoded)
        )
    return result


class Run:
    def __init__(self, root, mode, inputs, config, *, scope, parent_run_id=None):
        if mode not in MODES:
            raise ValueError(
                "Explicit current_clean/saved_input_audit/frozen_reproduction mode required"
            )
        self.root = safe_output(root)
        self.root.mkdir(parents=True, exist_ok=False)
        self.run_id = uuid4().hex
        self.mode = mode
        self.stages = []
        self.finalized = False
        records = []
        for role, source in inputs.items():
            if not role.replace("_", "").isalnum():
                raise ValueError("Invalid input role")
            target = self.root / "inputs" / (role + Path(source).suffix)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            records.append(
                {
                    **file_record(target, self.root),
                    "role": role,
                    "origin": "input_snapshot",
                    "upstream_run_id": parent_run_id,
                    "parser_version": "see_source_manifest",
                }
            )
        from shared.runtime.method_profile import method_identity

        config = {**config, "method_profile": method_identity()}
        write_json(self.root / "config.json", config)
        write_json(self.root / "environment.json", environment())
        manifest = {
            "schema": "entity.run_manifest.v3",
            "run_id": self.run_id,
            "parent_run_id": parent_run_id,
            "run_mode": mode,
            "created_at": now(),
            "scope": scope,
            "code": code_identity(),
            "inputs": records,
            "dependency_lock_scope": "runtime requirements; package file names recorded separately",
            "source_scope": "explicit input snapshots; upstream history not re-created",
            "scientific_confirmation": "pending_researcher_review",
        }
        write_json(self.root / "run_manifest.json", manifest)
        write_csv(self.root / "input_manifest.csv", records)

    def input(self, role):
        entry = next(
            (
                x
                for x in read_json(self.root / "run_manifest.json")["inputs"]
                if x["role"] == role
            )
        )
        return self.root / entry["path"]

    @contextmanager
    def stage(self, name, *, inputs=()):
        if self.finalized:
            raise ValueError("Run is already finalized")
        if not name.replace("_", "").isalnum() or name in self.stages:
            raise ValueError("Invalid or duplicate stage")
        self.stages.append(name)
        directory = self.root / "stages" / name
        directory.mkdir(parents=True, exist_ok=False)
        start = {
            "stage": name,
            "run_id": self.run_id,
            "started_at": now(),
            "inputs": [file_record(Path(p), self.root) for p in inputs],
        }
        write_json(directory / "started.json", start)
        result = {**start, "status": "failed"}
        from shared.runtime.stage_trace import ACTIVE_STAGE

        trace_token = ACTIVE_STAGE.set((directory, {**start, "stage_id": name}))
        try:
            from shared.validation.generation_audit import audit_scope

            with audit_scope(directory / "generation", self.run_id):
                yield directory
            result["status"] = "completed"
        except BaseException as exc:
            result["error_type"] = type(exc).__name__
            raise
        finally:
            ACTIVE_STAGE.reset(trace_token)
            result["finished_at"] = now()
            result["outputs"] = [
                file_record(p, self.root)
                for p in sorted(directory.rglob("*"))
                if p.is_file() and p.name not in ("started.json", "result.json")
            ]
            write_json(directory / "result.json", result)

    def finish(self, *, status="completed", gaps=()):
        if self.finalized:
            raise ValueError("Run is already finalized")
        if status not in ("completed", "completed_with_gaps", "failed"):
            raise ValueError("Invalid status")
        if any(
            (
                read_json(self.root / "stages" / name / "result.json")["status"]
                != "completed"
                for name in self.stages
            )
        ):
            status = "failed"
        planned = read_json(self.root / "config.json").get("planned_stages")
        if planned is not None and planned != self.stages:
            status = "failed"
        rows = [
            file_record(p, self.root)
            for p in sorted(self.root.rglob("*"))
            if p.is_file()
        ]
        config = read_json(self.root / "config.json")
        modes = {
            "run_mode": self.mode,
            "formula_id": config.get(
                "formula_id",
                config["method_profile"]["snapshot"]["ranking"]["formula_id"],
            ),
            "scoring_mode": config.get("scoring_mode", "not_declared_for_this_scope"),
            "generation_mode": config.get(
                "generation_mode", "see_stage_request_records"
            ),
        }
        rows = [
            {**row, **modes, "metadata_scope": "containing_run_not_historical_origin"}
            for row in rows
        ]
        write_csv(self.root / "output_manifest.csv", rows)
        write_csv(
            self.root / "artifact_manifest.csv",
            [
                {
                    **row,
                    "run_id": self.run_id,
                    "stage_name": row["path"].split("/")[1]
                    if row["path"].startswith("stages/")
                    else "run",
                    "artifact_id": self.run_id + "/" + row["path"],
                    "parent_artifact_id": self.run_id + "/run_manifest.json"
                    if row["path"] != "run_manifest.json"
                    else "",
                    "parent_relation": "run_membership_not_scientific_dependency",
                }
                for row in rows
            ],
        )
        result = {
            "schema": "entity.run_result.v3",
            "run_id": self.run_id,
            "run_mode": self.mode,
            "status": status,
            "finished_at": now(),
            "stages": self.stages,
            "gaps": list(gaps),
        }
        write_json(self.root / "run_result.json", result)
        verify(self.root)
        self.finalized = True
        return result


def verify(root, *, current_code=False):
    root = Path(root).resolve()
    manifest = read_json(root / "run_manifest.json")
    if (
        manifest["schema"] not in ("entity.run_manifest.v2", "entity.run_manifest.v3")
        or manifest["run_mode"] not in MODES
    ):
        raise ValueError("Unsupported manifest")
    names = [x["path"] for x in manifest["inputs"]] + [
        "config.json",
        "environment.json",
    ]
    for name in names:
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Missing or invalid artifact path: " + name)
    if (root / "run_result.json").exists():
        result = read_json(root / "run_result.json")
        if (
            result["run_id"] != manifest["run_id"]
            or result["run_mode"] != manifest["run_mode"]
        ):
            raise ValueError("Cross-run result mismatch")
    return manifest
