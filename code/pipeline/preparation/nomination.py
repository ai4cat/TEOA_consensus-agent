"""New candidate nomination with original prompt/schema and verifiable stage handoff."""

from pathlib import Path
import json, sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "code/pipeline"))
from preparation.entity_selection import (
    PROMPT,
    SCHEMA,
    expected_content,
    vocabulary_specs,
)
from shared.model_api.caller import ResilientCaller, read, save
from shared.runtime.streaming_chat import SSECollector
from runtime.receipts import validate_receipt


def prepare(runtime):
    folder = Path(runtime) / "prepared_prompts"
    receipt = folder / "completed.json"
    if receipt.exists():
        return validate_receipt(receipt)
    folder.mkdir(parents=True, exist_ok=True)
    if (ROOT / "inputs/01_prompts/prompt.txt").read_text(
        encoding="utf-8-sig"
    ).strip() != PROMPT:
        raise ValueError("Candidate prompt input and implementation differ")
    (folder / "candidate_instruction.txt").write_text(PROMPT + "\n", encoding="utf-8")
    (folder / "candidate_full_prompt.txt").write_text(
        expected_content(), encoding="utf-8"
    )
    save(folder / "candidate_schema.json", SCHEMA)
    save(
        folder / "request_settings.json",
        {
            "model": "explicit --candidate-model parameter",
            "temperature": 0,
            "max_tokens": 1800,
            "stream": True,
            "response_format": "strict json_schema",
            "is_original_network_record": False,
        },
    )
    value = {
        "contract": {"inputs": {str(ROOT / "inputs/01_prompts/prompt.txt"): {}}},
        "outputs": {str(p): {} for p in folder.iterdir() if p.is_file()},
        "state": "prepared",
        "new_model_calls": 0,
    }
    save(receipt, value)
    return value


def nominate(runtime, *, model, base, credential_pool):
    folder = Path(runtime) / "nomination"
    receipt = folder / "completed.json"
    prepared = prepare(runtime)
    contract = {"model": model, "base_url": base, "inputs": prepared["outputs"]}
    if receipt.exists():
        value = validate_receipt(receipt)
        if value["contract"] != contract:
            raise ValueError("Candidate stage configuration changed; use a new run")
        return value
    if not model:
        raise ValueError("Specify --candidate-model explicitly for fresh nomination")
    folder.mkdir(parents=True, exist_ok=True)
    from runtime.provider_credentials import load_provider_keys

    key = load_provider_keys({'credential_pool': credential_pool})[0]
    calls = folder / "model_calls"
    caller = ResilientCaller(
        calls,
        model,
        base,
        key,
        total_attempts=2,
        transport_attempts=2,
        format_repairs=0,
        accept_missing_finish=True,
    )
    value = caller.call("candidate_words", PROMPT, {}, SCHEMA, 1800)
    vocabulary_specs(value)
    ledger = read(calls / "request_ledger.json")
    accepted = [r for r in ledger.values() if r.get("status") == "completed"]
    if len(accepted) != 1:
        raise ValueError("Expected one completed nomination request")
    row = accepted[0]
    attempt = Path(row["attempts"][row["accepted_attempt"] - 1]).resolve()
    if not attempt.is_relative_to(calls.resolve()):
        raise ValueError("Candidate response outside current run")
    request = read(attempt / "request.json")
    transport = read(attempt / "transport.json")
    assert request["messages"] == [{"role": "user", "content": expected_content()}]
    assert (
        request["model"] == model
        and request["temperature"] == 0
        and (request["max_tokens"] == 1800)
        and (request["stream"] is True)
    )
    assert request["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "result", "strict": True, "schema": SCHEMA},
    }
    assert transport["http_status"] == 200
    collector = SSECollector(model)
    with (attempt / "response.sse").open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            collector.feed(chunk)
    body = collector.body(allow_missing_finish=True)
    assert (
        read(attempt / "response.body") == body
        and json.loads(body["choices"][0]["message"]["content"]) == value
    )
    assert read(attempt / "parsed_response.json") == value
    save(folder / "candidates.json", value)
    artifacts = [
        folder / "candidates.json",
        calls / "request_ledger.json",
        *sorted((p for p in attempt.iterdir() if p.is_file())),
    ]
    record = {
        "state": "completed",
        "contract": contract,
        "outputs": {str(p): {} for p in artifacts},
        "accepted_attempt": str(attempt),
        "candidate_count": len(value["terms"]),
        "calls_this_invocation": caller.attempts,
        "original_communications": str(calls / "calls"),
    }
    save(receipt, record)
    return record
