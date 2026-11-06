"""Persistent requests with separate transport and response-format recovery."""

import copy
import uuid
import json
import time
from datetime import datetime, timezone
from pathlib import Path
import jsonschema
import requests
from shared.runtime.streaming_chat import SSECollector, exception_details

MAX_RESPONSE_CHARACTERS = 120000


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(8):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(min(0.05 * 2**attempt, 1))


class RetryPending(RuntimeError):
    pass


class RequestBlocked(RuntimeError):
    pass


class ContractError(ValueError):
    pass


def validate_contract(value, payload, schema):
    jsonschema.validate(value, schema)
    props = schema.get("properties", {})
    for array in ("reviews", "corrections"):
        if array in props:
            id_key = (
                "field_id"
                if "field_id" in props[array]["items"]["properties"]
                else "unit_id"
            )
            expected = props[array]["items"]["properties"][id_key].get("enum")
            if expected is not None:
                ids = [r[id_key] for r in value[array]]
                if len(ids) != len(set(ids)) or set(ids) != set(expected):
                    raise ContractError("Missing or duplicate " + array + " unit IDs")
    if "corrections" in value and any(
        (not r["replacement"].strip() for r in value["corrections"])
    ):
        raise ContractError("Empty correction")
    if (
        isinstance(payload, dict)
        and "selectable_spans" in payload
        and ("reviews" in value)
    ):
        spans = {s["span_id"]: s for s in payload["selectable_spans"]}
        units = {u["unit_id"]: u for u in payload["units"]}
        for row in value["reviews"]:
            unit = units[row["unit_id"]]
            if not row["reason"].strip():
                raise ContractError("Adjudication needs rationale")
            for ref in row["evidence"]:
                span = spans.get(ref["span_id"])
                if (
                    span is None
                    or span["evidence_id"] != ref["evidence_id"]
                    or ref["evidence_id"] not in unit["source_ids"]
                ):
                    raise ContractError(
                        "Invalid/out-of-scope span receipt for " + row["unit_id"]
                    )
            if row["verdict"] == "acceptable":
                if row["issue"] in (
                    "unsupported_fact",
                    "contradiction",
                    "scope_ambiguity",
                ):
                    raise ContractError("Acceptance contradicts stated issue")
                if not row["evidence"] and unit["kind"] not in (
                    "hypothesis",
                    "proposed_experiment",
                ):
                    raise ContractError("Factual override requires source receipt")
                if not row["evidence"] and row["issue"] not in (
                    "reasonable_hypothesis",
                    "reasonable_proposal",
                ):
                    raise ContractError("Unsupported override")
    if "C" in props:
        expected = props["C"]["items"]["properties"]["id"].get("enum")
        if expected is not None and [c["id"] for c in value["C"]] != expected:
            raise ContractError("C IDs must occur exactly once in schema order")
    if (
        isinstance(payload, dict)
        and "axis" in payload
        and (value.get("axis") != payload["axis"])
    ):
        raise ContractError("Wrong analytical axis")
    facts = value.get("motivations", [])
    ids = [f["evidence_id"] for f in facts]
    if len(ids) != len(set(ids)):
        raise ContractError("Duplicate motivation evidence IDs")


def retry_kind(exc, receipt):
    if receipt.get("provider_error_code") in (
        "model_not_found",
        "invalid_api_key",
        "insufficient_quota",
        "model_not_allowed",
    ):
        return "configuration"
    status = receipt.get("http_status")
    if status in (401, 402, 403, 404):
        return "configuration"
    if status in (408, 429, 500, 502, 503, 504):
        return "transport"
    if isinstance(
        exc,
        (
            requests.Timeout,
            requests.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
        ),
    ):
        return "transport"
    transport_types = {
        "Timeout",
        "ReadTimeout",
        "ConnectTimeout",
        "ConnectionError",
        "ChunkedEncodingError",
        "ReadTimeoutError",
        "ConnectTimeoutError",
        "TimeoutError",
        "ConnectionResetError",
        "ProtocolError",
    }
    if any(
        (
            isinstance(x, dict) and x.get("type") in transport_types
            for x in receipt.get("exception_chain", [])
        )
    ):
        return "transport"
    text = " ".join(
        [
            str(exc),
            *(
                str(x.get("message", ""))
                for x in receipt.get("exception_chain", [])
                if isinstance(x, dict)
            ),
        ]
    )
    if isinstance(
        exc, (json.JSONDecodeError, jsonschema.ValidationError, ContractError)
    ):
        return "format"
    if receipt.get("finish_reason") == "length":
        return "format"
    if any(
        (
            s in text
            for s in (
                "Incomplete stream",
                "Provider emitted a stream error",
                "Request total deadline",
                "Repeated stream completion",
                "Data after stream completion",
            )
        )
    ):
        return "transport"
    return "fatal"


class ResilientCaller:
    def __init__(
        self,
        out,
        model,
        base,
        key,
        *,
        transport_attempts=3,
        format_repairs=2,
        total_attempts=None,
        sender=None,
        sleep=time.sleep,
        total_deadline=300,
        accept_missing_finish=False,
    ):
        self.out = Path(out)
        self.model = model
        self.base = base
        self.key = key
        self.transport_attempts = transport_attempts
        self.format_repairs = format_repairs
        self.total_attempts = total_attempts
        self.sender = sender or self.send
        self.sleep = sleep
        if (
            not isinstance(total_deadline, (int, float))
            or not 1 <= total_deadline <= 1800
        ):
            raise ValueError("total_deadline must be between 1 and 1800 seconds")
        self.total_deadline = total_deadline
        self.accept_missing_finish = bool(accept_missing_finish)
        self.index = (
            read(self.out / "request_ledger.json")
            if (self.out / "request_ledger.json").exists()
            else {}
        )
        self.attempts = 0

    @property
    def budget_exhausted(self):
        return self.total_attempts is not None and self.attempts >= self.total_attempts

    def send(self, request, folder):
        folder = Path(folder)
        start = time.monotonic()
        collector = SSECollector(self.model)
        receipt = {
            "status": "dispatched",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "response_origin": "derived from retained SSE",
            "input_characters": len(request["messages"][0]["content"]),
            "total_deadline_seconds": self.total_deadline,
            "read_timeout_seconds": 180,
            "proxy_policy": "direct",
        }

        def checkpoint():
            receipt.update(
                elapsed_seconds=round(time.monotonic() - start, 3),
                content_characters=sum(map(len, collector.parts)),
                events=collector.events,
                finish_reason=collector.finish,
                usage=collector.usage,
            )
            save(folder / "transport.json", receipt)

        checkpoint()
        tail = b""
        last_checkpoint = start
        try:
            with requests.post(
                self.base + "/chat/completions",
                json=request,
                headers={"Authorization": "Bearer " + self.key},
                timeout=(20, 180),
                stream=True,
                allow_redirects=False,
                proxies={"http": "", "https": "", "all": ""},
            ) as response:
                receipt["http_status"] = response.status_code
                sensitive = (
                    "authorization",
                    "cookie",
                    "token",
                    "secret",
                    "api-key",
                    "api_key",
                )
                redacted = [
                    str(k)
                    for k in response.headers
                    if any((t in str(k).lower() for t in sensitive))
                ]
                headers = {
                    str(k): "[REDACTED]" if str(k) in redacted else str(v)
                    for k, v in response.headers.items()
                }
                ids = {
                    k: v
                    for k, v in headers.items()
                    if k.lower()
                    in (
                        "x-request-id",
                        "request-id",
                        "x-oneapi-request-id",
                        "x-ms-request-id",
                        "openai-request-id",
                    )
                }
                receipt.update(
                    response_headers=headers,
                    response_headers_redacted=redacted,
                    http_request_ids=ids,
                    http_request_id_status="recorded"
                    if ids
                    else "not_present_in_response_headers",
                    headers_seconds=time.monotonic() - start,
                )
                checkpoint()
                if response.status_code != 200:
                    raise RuntimeError("HTTP " + str(response.status_code))
                if (
                    "text/event-stream"
                    not in response.headers.get("Content-Type", "").lower()
                ):
                    raise RuntimeError("Expected SSE transport")
                with (folder / "response.sse").open("xb") as wire:
                    for chunk in response.iter_content(1024):
                        if not chunk:
                            continue
                        if self.key.encode() in tail + chunk:
                            raise RuntimeError("Sensitive response withheld")
                        tail = (tail + chunk)[-max(len(self.key) - 1, 1) :]
                        wire.write(chunk)
                        wire.flush()
                        collector.feed(chunk)
                        if wire.tell() > 16000000:
                            raise RuntimeError("Response byte budget exceeded")
                        if time.monotonic() - start > self.total_deadline:
                            raise RuntimeError("Request total deadline exceeded")
                        if time.monotonic() - last_checkpoint >= 5:
                            checkpoint()
                            last_checkpoint = time.monotonic()
            body = collector.body(allow_missing_finish=self.accept_missing_finish)
            save(folder / "response.body", body)
            if sum(map(len, collector.parts)) > MAX_RESPONSE_CHARACTERS:
                raise RuntimeError("Response character budget exceeded")
            receipt["status"] = (
                "completed" if collector.finish == "stop" else "received_unknown_finish"
            )
            if collector.finish is None:
                receipt["acceptance_policy"] = (
                    "explicit_opt_in_DONE_nonempty_matching_model; structured_validation_required"
                )
            checkpoint()
            return (body, receipt)
        except Exception as exc:
            receipt.update(
                status="failed", exception_chain=exception_details(exc, self.key)
            )
            checkpoint()
            raise

    def call(self, name, instruction, payload, schema, max_tokens=2200):
        signature = uuid.uuid4().hex
        logical = name + "_" + signature
        cache = self.out / "cache" / (logical + ".json")
        entry = self.index.setdefault(
            logical, {"logical_stage": name, "signature": signature, "attempts": []}
        )
        if entry.get("status") == "completed":
            value = read(cache)
            validate_contract(value, payload, schema)
            return value
        request = {
            "model": self.model,
            "temperature": 0,
            "stream": True,
            "max_tokens": max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": instruction
                    + "\nINPUT_JSON\n"
                    + json.dumps(payload, ensure_ascii=False)
                    + "\nOUTPUT_JSON_SCHEMA\n"
                    + json.dumps(schema, ensure_ascii=False),
                }
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": True, "schema": schema},
            },
        }
        base_request = copy.deepcopy(request)
        transport_failures = 0
        format_failures = 0
        while True:
            if self.budget_exhausted:
                entry.update(status="awaiting_retry", reason="request budget reached")
                save(self.out / "request_ledger.json", self.index)
                raise RetryPending("Request budget reached; completed stages retained")
            number = len(entry["attempts"]) + 1
            folder = self.out / "calls" / logical / f"attempt_{number}"
            folder.mkdir(parents=True, exist_ok=False)
            save(folder / "request.json", request)
            self.attempts += 1
            entry["attempts"].append(str(folder))
            entry["status"] = "running"
            save(self.out / "request_ledger.json", self.index)
            print(name + ": request " + str(number), flush=True)
            receipt = {}
            body = None
            try:
                body, receipt = self.sender(request, folder)
                value = json.loads(body["choices"][0]["message"]["content"])
                validate_contract(value, payload, schema)
                if receipt.get("status") == "received_unknown_finish":
                    receipt["structured_validation"] = "passed"
                    save(folder / "transport.json", receipt)
                save(cache, value)
                save(folder / "parsed_response.json", value)
                entry.update(
                    status="completed",
                    accepted_attempt=number,
                    format_repaired=format_failures > 0,
                    origin={"source": str(folder / "response.body")},
                    finish_reason=receipt.get("finish_reason"),
                    transport_status=receipt.get("status"),
                )
                save(self.out / "request_ledger.json", self.index)
                return value
            except Exception as exc:
                if (folder / "transport.json").exists():
                    receipt = read(folder / "transport.json")
                kind = retry_kind(exc, receipt)
                if (
                    isinstance(exc, json.JSONDecodeError)
                    and body is None
                    and (receipt.get("http_status") == 200)
                ):
                    kind = "transport"
                save(
                    folder / "recovery.json",
                    {
                        "kind": kind,
                        "error": exception_details(exc, self.key),
                        "transport_failure_does_not_change_prompt": kind == "transport",
                    },
                )
                if kind == "configuration":
                    entry.update(
                        status="blocked_configuration",
                        error=exception_details(exc, self.key),
                    )
                    save(self.out / "request_ledger.json", self.index)
                    raise RequestBlocked("API configuration rejected") from exc
                if kind == "transport":
                    transport_failures += 1
                    if transport_failures < self.transport_attempts:
                        self.sleep(min(2**transport_failures, 8))
                        continue
                elif kind == "format":
                    format_failures += 1
                    if format_failures <= self.format_repairs:
                        previous = (
                            body["choices"][0]["message"]["content"] if body else ""
                        )
                        request = copy.deepcopy(base_request)
                        request["messages"][0]["content"] += (
                            "\nFORMAT_RECOVERY\n"
                            + json.dumps(
                                {
                                    "error": str(exc)[:1000],
                                    "previous_response": previous,
                                    "instruction": "The previous complete response did not satisfy the JSON/schema contract. Return one valid JSON object. Preserve source scope, uncertainty and substantive content. For review outputs, assess the original supplied sentences; never fill missing verdicts with acceptance by default. No desired experimental result is supplied.",
                                },
                                ensure_ascii=False,
                            )
                        )
                        continue
                entry.update(
                    status="awaiting_retry"
                    if kind in ("transport", "format")
                    else "needs_attention",
                    error=exception_details(exc, self.key),
                    reason=kind,
                )
                save(self.out / "request_ledger.json", self.index)
                if kind in ("transport", "format"):
                    raise RetryPending("Stage can resume: " + name) from exc
                raise
