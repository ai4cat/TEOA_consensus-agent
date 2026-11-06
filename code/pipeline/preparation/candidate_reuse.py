"""Explicit reuse of a retained, successful candidate nomination; no model call."""
import json
import shutil
from pathlib import Path

from preparation.entity_selection import expected_content, vocabulary_specs


def load_successful_candidates(source_run):
    root = Path(source_run).resolve()
    output = root / "outputs/02_candidates"
    if not output.is_dir():
        output = root / "outputs/01_candidates"
    read = lambda p: json.loads(p.read_text(encoding="utf-8-sig"))
    candidates = read(output / "candidates.json")
    vocabulary_specs(candidates)
    provenance = read(output / "nomination_provenance.json")
    attempt = (root / provenance["accepted_attempt"]).resolve()
    if not attempt.is_relative_to(root / "communications" / output.name):
        raise ValueError("Candidate response must be inside the source run's candidate communications")
    parsed = read(attempt / "parsed_response.json")
    request = read(attempt / "request.json")
    transport = read(attempt / "transport.json")
    body = read(attempt / "response.body")
    response = json.loads(body["choices"][0]["message"]["content"])
    if parsed != candidates or response != candidates:
        raise ValueError("Candidate table differs from the retained response")
    if transport.get("http_status") != 200 or transport.get("status") != "completed":
        raise ValueError("Candidate response has no successful completion record")
    if request.get("messages") != [{"role": "user", "content": expected_content()}]:
        raise ValueError("Cached candidate request does not match the current nomination prompt")
    if request.get("model") != provenance.get("model"):
        raise ValueError("Candidate model metadata mismatch")
    return candidates, {
        "mode": "explicit_successful_candidate_reuse",
        "source_run": str(root),
        "source_candidate_file": str(output / "candidates.json"),
        "source_accepted_attempt": str(attempt),
        "model": request["model"],
        "candidate_count": len(candidates["terms"]),
        "new_model_requests": 0,
        "validation": "Schema, retained request, successful response and table equality; no content digests",
    }


def reuse_candidates(ctx, source_run):
    candidates, record = load_successful_candidates(source_run)
    destination = ctx.communications / "reused_candidate_response"
    destination.mkdir(parents=True, exist_ok=True)
    attempt = Path(record["source_accepted_attempt"])
    for name in ("request.json", "parsed_response.json", "response.body", "response.sse", "transport.json"):
        shutil.copy2(attempt / name, destination / name)
    record["accepted_attempt"] = destination.relative_to(ctx.run).as_posix()
    ctx.emit("candidates.json", candidates)
    ctx.emit("nomination_provenance.json", record)

