"""Bounded request transport with unique invocation records and no cache reuse."""

import asyncio, json, os, threading, time, uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from datetime import datetime, timezone
from pathlib import Path
import requests
from shared.verified_tls import create_verified_context, VerifiedContextAdapter
from shared.runtime.streaming_chat import SSECollector


def available_memory():
    if os.name != "nt":
        return None
    import ctypes

    class Memory(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
            (n, ctypes.c_ulonglong)
            for n in (
                "total",
                "available",
                "page_total",
                "page_available",
                "virtual_total",
                "virtual_available",
                "extended",
            )
        ]

    value = Memory()
    value.length = ctypes.sizeof(value)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
        return None
    return value.available


def now():
    return datetime.now(timezone.utc).isoformat()


def request_timeouts(stage, options):
    """Allow a long merge request to wait without changing other task policies."""
    import math

    connect = 15 if stage == "claims-3B" else 30
    read = float(options.get("read_timeout_seconds", 180 if stage == "claims-3B" else 600))
    deadline = float(options.get("response_deadline_seconds", 600 if stage == "claims-3B" else 1800))
    if not all(math.isfinite(value) and value > 0 for value in (read, deadline)) or deadline < read:
        raise ValueError("Request timeouts must be positive and deadline must cover read timeout")
    return connect, read, deadline


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + str(threading.get_ident()) + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    # Windows readers/antivirus can briefly deny atomic replacement.
    # Durable scientific receipts still fail explicitly if bounded retries expire.
    for attempt in range(100):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == 99:
                raise
            time.sleep(0.05)


class RequestTransport:
    def __init__(
        self,
        root,
        keys,
        *,
        initial=4,
        maximum=16,
        base_url="",
    ):
        if not base_url:
            raise ValueError("An explicit model provider base_url is required")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.keys = keys
        self.base = base_url.rstrip("/")
        self.stage = "preparing"
        self.ssl_context = create_verified_context()
        self.cv = threading.Condition()
        self.limit = initial
        self.maximum = maximum
        self.executor = ThreadPoolExecutor(
            max_workers=maximum, thread_name_prefix="model_request"
        )
        self.active = 0
        self.recent = deque(maxlen=100)
        self.since_adjust = 0
        self.slot = 0
        self.stats = {
            "pid": os.getpid(),
            "started": now(),
            "started_requests": 0,
            "completed_requests": 0,
            "failed_requests": 0,
            "reused_requests": 0,
            "inflight": 0,
            "peak": 0,
            "key_slots": {
                str(i + 1): {"started": 0, "completed": 0, "failed": 0}
                for i in range(len(keys))
            },
        }

    def status(self):
        # Progress is telemetry, not a scientific commit. Never turn a successful
        # paid response into a failed source because another process reads it.
        with self.cv:
            stamp = time.monotonic()
            if stamp < getattr(self, "_next_status", 0) and self.active:
                return
            self._next_status = stamp + 0.25
            self.stats.update(
                stage=self.stage,
                updated=now(),
                inflight=self.active,
                limit=self.limit,
                maximum=self.maximum,
                recent_count=len(self.recent),
                recent_failure_rate=(1 - sum(self.recent) / len(self.recent))
                if self.recent
                else None,
            )
            path = self.root.parent / "transport_progress.json"
            temp = path.with_name(path.name + "." + str(threading.get_ident()) + ".tmp")
            try:
                temp.write_text(
                    json.dumps(self.stats, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                os.replace(temp, path)
            except OSError as exc:
                self.stats["telemetry_write_errors"] = (
                    self.stats.get("telemetry_write_errors", 0) + 1
                )
                self.stats["telemetry_last_error_type"] = type(exc).__name__

    def set_stage(self, stage):
        self.stage = stage
        self.status()

    def acquire(self):
        with self.cv:
            while True:
                memory = available_memory()
                effective = (
                    min(self.limit, 8)
                    if memory is not None and memory < 4 * 1024**3
                    else self.limit
                )
                self.stats["available_memory_bytes"] = memory
                self.stats["effective_limit"] = effective
                if self.active < effective and (memory is None or memory >= 1024**3):
                    break
                self.cv.wait(1)
            self.active += 1
            self.stats["peak"] = max(self.stats["peak"], self.active)
            index = self.slot % len(self.keys)
            self.slot += 1
            self.stats["started_requests"] += 1
            self.stats["key_slots"][str(index + 1)]["started"] += 1
            self.status()
            return index

    def release(self, index, success, **outcome):
        with self.cv:
            self.active -= 1
            self.recent.append(bool(success))
            self.since_adjust += 1
            key = "completed" if success else "failed"
            self.stats[key + "_requests"] += 1
            self.stats["key_slots"][str(index + 1)][key] += 1
            if (
                self.since_adjust >= 32
                and len(self.recent) >= 32
                and 1 - sum(self.recent) / len(self.recent) >= 0.30
            ):
                self.limit = max(2, self.limit // 2)
                self.since_adjust = 0
            elif (
                self.since_adjust >= 100
                and len(self.recent) == 100
                and sum(self.recent) >= 98
            ):
                self.limit = min(self.maximum, self.limit + 8)
                self.since_adjust = 0
            self.cv.notify_all()
            self.status()

    def request(self, api_key, base_url, model_name, prompt, **kwargs):
        # Credential values never participate in saved request identities.
        model = "gpt-5.4-mini" if self.stage == "claims-3A" else model_name
        tokens = int(kwargs.get("max_output_tokens", 3200))
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": tokens,
            "response_format": {"type": "json_object"},
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self.stage == "claims-3B":
            payload.update(reasoning_effort="medium", max_tokens=65536)
        wire = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        url = (base_url or self.base).rstrip("/") + "/chat/completions"
        identity = uuid.uuid4().hex
        if self.stage in {
            s.strip()
            for s in os.getenv("PIPELINE_CACHE_ONLY_STAGES", "").split(",")
            if s.strip()
        }:
            raise RuntimeError(
                "Request caching is disabled; cache-only execution is unavailable"
            )
        folder = self.root / identity
        success = folder / "success.json"
        folder.mkdir(parents=True, exist_ok=True)
        # Stage 5 should retry a stalled provider promptly, without limiting
        # parallel admission or shortening another stage's extraction policy.
        connect_timeout, read_timeout, response_deadline = request_timeouts(self.stage, kwargs)
        max_attempts = 2 if self.stage == "claims-3B" else 3
        # Unfinished attempts from a dead process remain preserved. Caller-level run lock prevents concurrent replay.
        for number in range(1, max_attempts + 1):
            attempt = folder / f"attempt_{number:03d}"
            attempt.mkdir(exist_ok=False)
            (attempt / "request.json").write_bytes(wire)
            index = self.acquire()
            start = time.monotonic()
            passed = False
            receipt = {
                "state": "awaiting_headers",
                "url": url,
                "identity": identity,
                "stage": self.stage,
                "model": model,
                "key_slot": index + 1,
                "started": now(),
                "pid": os.getpid(),
                "connect_timeout_seconds": connect_timeout,
                "read_idle_timeout_seconds": read_timeout,
                "response_deadline_seconds": response_deadline,
                "max_transport_attempts": max_attempts,
            }
            write(attempt / "transport.json", receipt)
            length_retry = False
            delay = min(2**number, 30)
            try:
                collector = SSECollector(model)
                with requests.Session() as session:
                    session.trust_env = False
                    session.mount("https://", VerifiedContextAdapter(self.ssl_context))
                    with session.post(
                        url,
                        data=wire,
                        headers={
                            "Authorization": "Bearer " + self.keys[index],
                            "Content-Type": "application/json",
                        },
                        stream=True,
                        timeout=(connect_timeout, read_timeout),
                        allow_redirects=False,
                    ) as response:
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
                            if any(t in str(k).lower() for t in sensitive)
                        ]
                        headers = {
                            str(k): ("[REDACTED]" if str(k) in redacted else str(v))
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
                            http_status=response.status_code,
                            headers_seconds=time.monotonic() - start,
                            response_headers=headers,
                            response_headers_redacted=redacted,
                            http_request_ids=ids,
                            http_request_id_status="recorded"
                            if ids
                            else "not_present_in_response_headers",
                        )
                        write(attempt / "transport.json", receipt)
                        if response.status_code != 200:
                            try:
                                delay = max(
                                    delay,
                                    float(response.headers.get("Retry-After", "0")),
                                )
                            except ValueError:
                                pass
                            raise requests.HTTPError(
                                "HTTP " + str(response.status_code)
                            )
                        if "text/event-stream" not in response.headers.get(
                            "Content-Type", ""
                        ):
                            raise ValueError("Expected event stream")
                        with (attempt / "response.sse").open("xb") as handle:
                            for part in response.iter_content(1024):
                                if not part:
                                    continue
                                receipt.setdefault(
                                    "first_byte_seconds", time.monotonic() - start
                                )
                                handle.write(part)
                                handle.flush()
                                collector.feed(part)
                                if collector.done:
                                    break
                                if time.monotonic() - start > response_deadline:
                                    raise TimeoutError("Response exceeded deadline")
                body = collector.body(allow_missing_finish=True)
                content = body["choices"][0]["message"]["content"]
                json.loads(content)
                receipt.update(
                    state="completed",
                    completed_at=now(),
                    finish_reason=collector.finish,
                    usage=collector.usage,
                    completion_basis="explicit_stop"
                    if collector.finish == "stop"
                    else "terminal_DONE_and_complete_JSON",
                    elapsed_seconds=time.monotonic() - start,
                )
                write(attempt / "response.json", body)
                write(attempt / "transport.json", receipt)
                write(success, {**receipt, "attempt": attempt.name})
                passed = True
                return content
            except Exception as exc:
                receipt.update(
                    state="failed",
                    error_type=type(exc).__name__,
                    elapsed_seconds=time.monotonic() - start,
                    finish_reason=collector.finish if "collector" in locals() else None,
                )
                write(attempt / "transport.json", receipt)
                if receipt.get("finish_reason") == "length":
                    length_retry = True
                elif (
                    receipt.get("http_status") in (400, 401, 402, 403, 404)
                    or number == max_attempts
                ):
                    raise
            finally:
                self.release(
                    index,
                    passed,
                    elapsed=time.monotonic() - start,
                    http_status=receipt.get("http_status"),
                    retry_after=delay,
                    error_type=receipt.get("error_type", ""),
                )
            if length_retry:
                return self._retry_length(
                    api_key, base_url, model_name, prompt, tokens, kwargs
                )
            time.sleep(delay)

    def _retry_length(self, api_key, base_url, model_name, prompt, tokens, kwargs):
        # Only 3A's observed explicit truncation is covered by this bounded policy.
        # Base requests are unchanged; higher-budget responses have separate invocation records.
        if self.stage != "claims-3A" or tokens >= 12800:
            raise ValueError(
                "Explicit output truncation exceeds bounded recovery policy"
            )
        revised = dict(kwargs)
        revised["max_output_tokens"] = min(tokens * 2, 12800)
        return self.request(api_key, base_url, model_name, prompt, **revised)

    async def request_async(self, *args, **kwargs):
        # Keep network workers out of asyncio's checkpoint-writing executor.
        return await asyncio.get_running_loop().run_in_executor(
            self.executor, partial(self.request, *args, **kwargs)
        )
