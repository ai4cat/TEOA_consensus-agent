from __future__ import annotations

import json
import re
import time

from shared.llm.transport import _request_responses_api


def _call_llm(
    prompt: str, llm_env: dict[str, str], *, max_tokens: int
) -> dict[str, object]:
    started = time.time()
    prompt = _strip_control_chars(prompt)
    try:
        content = _request_responses_api(
            api_key=str(llm_env["LLM_API_KEY"]),
            base_url=str(llm_env["LLM_API_BASE_URL"]),
            model_name=str(llm_env["LLM_MODEL_NAME"]),
            prompt=prompt,
            max_output_tokens=max_tokens,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "elapsed": time.time() - started,
            "error": f"{type(exc).__name__}: {exc}",
            "raw": "",
            "prompt_chars": len(prompt),
        }
    elapsed = time.time() - started
    payload = _parse_json_object(content)
    if payload is None:
        return {
            "elapsed": elapsed,
            "error": "json_parse_failed",
            "raw": content,
            "prompt_chars": len(prompt),
        }
    if "evidence_score" in payload:
        payload["evidence_score"] = _to_int(payload.get("evidence_score"))
    if "impact_score" in payload:
        payload["impact_score"] = _to_int(payload.get("impact_score"))
    payload["elapsed"] = elapsed
    payload["raw"] = content
    payload["prompt_chars"] = len(prompt)
    return payload


def _strip_control_chars(text: str) -> str:
    return "".join(ch if ch in "\n\r\t" or ord(ch) >= 32 else " " for ch in text)


def _parse_json_object(content: str) -> dict[str, object] | None:
    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return obj if isinstance(obj, dict) else None


def _to_int(value: object) -> int | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return None
