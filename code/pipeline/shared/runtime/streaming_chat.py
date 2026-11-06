"""One authenticated request, exact SSE retention, and verifiable derived JSON."""

import json
import re


def exception_details(exc, secret=""):
    rows = []
    seen = set()

    def visit(e):
        if not isinstance(e, BaseException) or id(e) in seen:
            return
        seen.add(id(e))
        message = str(e)
        if secret:
            message = message.replace(secret, "[REDACTED]")
        message = re.sub(
            "(?i)(authorization|api[_-]?key|token)\\s*[:=]\\s*\\S+",
            "\\1=[REDACTED]",
            message,
        )
        rows.append({"type": type(e).__name__, "message": message[:1200]})
        for child in [e.__cause__, e.__context__, getattr(e, "reason", None), *e.args]:
            visit(child)

    visit(exc)
    return rows


class SSECollector:
    def __init__(self, model):
        self.model = model
        self.buffer = b""
        self.parts = []
        self.done = False
        self.finish = None
        self.usage = None
        self.events = 0
        self.comments = 0
        self.returned = False

    def feed(self, chunk):
        self.buffer += chunk
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if line.startswith(b":"):
                self.comments += 1
            if not line.startswith(b"data:"):
                continue
            value = line[5:].strip()
            if value == b"[DONE]":
                if self.done:
                    raise ValueError("Repeated stream completion")
                self.done = True
                continue
            if not value:
                continue
            if self.done:
                raise ValueError("Data after stream completion")
            event = json.loads(value)
            self.events += 1
            if event.get("error"):
                raise ValueError("Provider emitted a stream error")
            model = event.get("model")
            if model is not None:
                if model != self.model:
                    raise ValueError("Unexpected returned model")
                self.returned = True
            if event.get("usage") is not None:
                self.usage = event["usage"]
            for choice in event.get("choices", []):
                if choice.get("index", 0) != 0:
                    raise ValueError("Multiple stream choices not supported")
                delta = choice.get("delta", {})
                if delta.get("tool_calls") or delta.get("refusal"):
                    raise ValueError("Unexpected tool/refusal response")
                content = delta.get("content")
                if content is not None:
                    if not isinstance(content, str) or self.finish is not None:
                        raise ValueError("Invalid stream content")
                    self.parts.append(content)
                finish = choice.get("finish_reason")
                if finish is not None:
                    if self.finish is not None:
                        raise ValueError("Repeated finish reason")
                    self.finish = finish

    def body(self, *, allow_missing_finish=False):
        if self.buffer.strip():
            self.feed(b"\n")
        if (
            not self.done
            or not self.returned
            or (
                not (
                    self.finish == "stop"
                    or (allow_missing_finish and self.finish is None)
                )
            )
            or (not "".join(self.parts).strip())
        ):
            raise ValueError("Incomplete stream; completion and stop required")
        return {
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": self.finish,
                    "message": {"role": "assistant", "content": "".join(self.parts)},
                }
            ],
            "usage": self.usage,
        }
