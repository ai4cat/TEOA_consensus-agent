"""HTTP transport, response decoding, retries and usage records."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import threading
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

try:
    import httpx
except ImportError:
    httpx = None


_SSL_CONTEXT_CACHE = {}
_SSL_CONTEXT_LOCK = threading.Lock()


def _shared_ssl_context(*, verify: bool, trust_env: bool):
    """Reuse HTTPX's identical TLS policy, without reloading roots for every task."""
    from httpx._config import create_ssl_context
    import certifi

    paths = [certifi.where()]
    if trust_env:
        paths.extend(os.getenv(name, "") for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"))
    identities = []
    for path in paths:
        try:
            stat = os.stat(path) if path else None
            identities.append((path, stat.st_mtime_ns if stat else None, stat.st_size if stat else None))
        except OSError:
            identities.append((path, None, None))
    key = (verify, trust_env, tuple(identities))
    with _SSL_CONTEXT_LOCK:
        if key not in _SSL_CONTEXT_CACHE:
            _SSL_CONTEXT_CACHE[key] = create_ssl_context(verify=verify, trust_env=trust_env)
        return _SSL_CONTEXT_CACHE[key]


def _request_responses_api(
    api_key: str,
    base_url: str,
    model_name: str,
    prompt: str,
    *,
    max_output_tokens: int = 300,
    json_mode: bool = True,
) -> str:
    if _is_local_ollama_base_url(base_url):
        return _request_ollama_native_chat(
            api_key=api_key,
            base_url=base_url,
            model_name=model_name,
            prompt=prompt,
            max_output_tokens=max_output_tokens,
            json_mode=json_mode,
        )

    if _should_use_openai_responses_api(model_name=model_name):
        return _request_openai_responses_api(
            api_key=api_key,
            base_url=base_url,
            model_name=model_name,
            prompt=prompt,
            max_output_tokens=max_output_tokens,
            json_mode=json_mode,
        )

    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_output_tokens,
    }
    if _should_use_minimal_reasoning(model_name):
        payload["reasoning_effort"] = "minimal"
    if _should_disable_thinking(base_url=base_url, model_name=model_name):
        payload["think"] = False
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Connection": "close",
    }
    url = _openai_chat_completions_url(base_url)
    retryable_request_errors = (
        requests.exceptions.SSLError,
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    )

    last_error: RuntimeError | None = None
    for attempt in range(1, 4):
        try:
            response = requests.post(
                url,
                headers=headers,
                data=json.dumps(payload),
                timeout=_request_timeout_seconds(base_url=base_url),
            )
        except retryable_request_errors as exc:
            last_error = RuntimeError(
                f"PROVIDER_REQUEST_FAILED: POST {url} failed: {exc}"
            )
            if attempt == 3:
                raise last_error from exc
            print(f"[WARN] provider transport error on attempt {attempt}/3: {exc}")
            time.sleep(attempt * 2)
            continue
        except requests.RequestException as exc:
            raise RuntimeError(
                f"PROVIDER_REQUEST_FAILED: POST {url} failed: {exc}"
            ) from exc

        if response.status_code in {429, 500, 502, 503, 504}:
            last_error = RuntimeError(
                f"PROVIDER_HTTP_ERROR: POST {url} status={response.status_code} body={response.text[:500]}"
            )
            if attempt == 3:
                raise last_error
            print(
                f"[WARN] provider HTTP retry on attempt {attempt}/3: status={response.status_code}"
            )
            time.sleep(attempt * 2)
            continue

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(
                f"PROVIDER_HTTP_ERROR: POST {url} status={response.status_code} body={response.text[:500]}"
            ) from exc

        response_text = response.text or ""
        extracted_text = _extract_openai_compatible_text_from_response_body(
            response_text
        )
        if extracted_text:
            _append_llm_usage_event(
                route="openai_compatible_chat_completions",
                base_url=base_url,
                request_url=url,
                model_name=model_name,
                prompt=prompt,
                response_text=extracted_text,
                usage_payload=None,
            )
            return extracted_text

        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"PROVIDER_INVALID_JSON: POST {url} returned non-JSON body={response_text[:500]}"
            ) from exc

        extracted_text = _extract_chat_completions_text(data)
        if extracted_text:
            _append_llm_usage_event(
                route="openai_compatible_chat_completions",
                base_url=base_url,
                request_url=url,
                model_name=model_name,
                prompt=prompt,
                response_text=extracted_text,
                usage_payload=data.get("usage"),
            )
            return extracted_text
        raise RuntimeError(
            f"PROVIDER_EMPTY_OUTPUT: {url} returned no assistant content."
        )

    if last_error is not None:
        raise last_error
    raise RuntimeError(
        f"PROVIDER_REQUEST_FAILED: POST {url} failed for an unknown reason."
    )


async def _request_responses_api_async(
    api_key: str,
    base_url: str,
    model_name: str,
    prompt: str,
    *,
    max_output_tokens: int = 300,
    json_mode: bool = True,
) -> str:
    if httpx is None:
        return await asyncio.to_thread(
            _request_responses_api,
            api_key,
            base_url,
            model_name,
            prompt,
            max_output_tokens=max_output_tokens,
            json_mode=json_mode,
        )
    if _is_local_ollama_base_url(base_url):
        return await asyncio.to_thread(
            _request_ollama_native_chat,
            api_key=api_key,
            base_url=base_url,
            model_name=model_name,
            prompt=prompt,
            max_output_tokens=max_output_tokens,
            json_mode=json_mode,
        )

    if _should_use_openai_responses_api(model_name=model_name):
        return await asyncio.to_thread(
            _request_openai_responses_api,
            api_key=api_key,
            base_url=base_url,
            model_name=model_name,
            prompt=prompt,
            max_output_tokens=max_output_tokens,
            json_mode=json_mode,
        )

    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_output_tokens,
    }
    if _should_use_minimal_reasoning(model_name):
        payload["reasoning_effort"] = "minimal"
    if _should_disable_thinking(base_url=base_url, model_name=model_name):
        payload["think"] = False
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Connection": "close",
    }
    url = _openai_chat_completions_url(base_url)
    timeout = httpx.Timeout(_request_timeout_seconds(base_url=base_url))
    trust_env = str(os.getenv("LLM_HTTPX_TRUST_ENV", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    verify = str(os.getenv("LLM_HTTPX_VERIFY", "true")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    retryable_request_errors = (
        httpx.ConnectError,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.ConnectTimeout,
        httpx.RemoteProtocolError,
    )

    last_error: RuntimeError | None = None
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(
                timeout=timeout, trust_env=trust_env,
                verify=_shared_ssl_context(verify=verify, trust_env=trust_env)
            ) as client:
                response = await client.post(url, headers=headers, json=payload)
        except retryable_request_errors as exc:
            last_error = RuntimeError(
                f"PROVIDER_REQUEST_FAILED: POST {url} failed: {exc}"
            )
            if attempt == 3:
                raise last_error from exc
            print(f"[WARN] provider transport error on attempt {attempt}/3: {exc}")
            await asyncio.sleep(attempt * 2)
            continue
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"PROVIDER_REQUEST_FAILED: POST {url} failed: {exc}"
            ) from exc

        if response.status_code in {429, 500, 502, 503, 504}:
            last_error = RuntimeError(
                f"PROVIDER_HTTP_ERROR: POST {url} status={response.status_code} body={response.text[:500]}"
            )
            if attempt == 3:
                raise last_error
            print(
                f"[WARN] provider HTTP retry on attempt {attempt}/3: status={response.status_code}"
            )
            await asyncio.sleep(attempt * 2)
            continue

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"PROVIDER_HTTP_ERROR: POST {url} status={response.status_code} body={response.text[:500]}"
            ) from exc

        response_text = response.text or ""
        extracted_text = _extract_openai_compatible_text_from_response_body(
            response_text
        )
        if extracted_text:
            _append_llm_usage_event(
                route="openai_compatible_chat_completions_async",
                base_url=base_url,
                request_url=url,
                model_name=model_name,
                prompt=prompt,
                response_text=extracted_text,
                usage_payload=None,
            )
            return extracted_text

        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"PROVIDER_INVALID_JSON: POST {url} returned non-JSON body={response_text[:500]}"
            ) from exc

        extracted_text = _extract_chat_completions_text(data)
        if extracted_text:
            _append_llm_usage_event(
                route="openai_compatible_chat_completions_async",
                base_url=base_url,
                request_url=url,
                model_name=model_name,
                prompt=prompt,
                response_text=extracted_text,
                usage_payload=data.get("usage"),
            )
            return extracted_text
        raise RuntimeError(
            f"PROVIDER_EMPTY_OUTPUT: {url} returned no assistant content."
        )

    if last_error is not None:
        raise last_error
    raise RuntimeError(
        f"PROVIDER_REQUEST_FAILED: POST {url} failed for an unknown reason."
    )


def _request_openai_responses_api(
    *,
    api_key: str,
    base_url: str,
    model_name: str,
    prompt: str,
    max_output_tokens: int,
    json_mode: bool,
) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Connection": "close",
    }
    url = _openai_responses_url(base_url)
    timeout = _request_timeout_seconds(base_url=base_url)
    verify = str(os.getenv("LLM_REQUESTS_VERIFY", "true")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    use_background = str(os.getenv("LLM_RESPONSES_BACKGROUND", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    payload = _build_openai_responses_payload(
        model_name=model_name,
        prompt=prompt,
        max_output_tokens=max_output_tokens,
        json_mode=json_mode,
        include_json_format=json_mode,
        use_background=use_background,
    )
    retryable_request_errors = (
        requests.exceptions.SSLError,
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    )
    last_error: RuntimeError | None = None
    retried_without_json_format = False

    for attempt in range(1, 4):
        try:
            response = requests.post(
                url,
                headers=headers,
                data=json.dumps(payload),
                timeout=timeout,
                verify=verify,
            )
        except retryable_request_errors as exc:
            last_error = RuntimeError(
                f"PROVIDER_REQUEST_FAILED: POST {url} failed: {exc}"
            )
            if attempt == 3:
                raise last_error from exc
            print(f"[WARN] provider transport error on attempt {attempt}/3: {exc}")
            time.sleep(attempt * 2)
            continue
        except requests.RequestException as exc:
            raise RuntimeError(
                f"PROVIDER_REQUEST_FAILED: POST {url} failed: {exc}"
            ) from exc

        if (
            response.status_code == 400
            and json_mode
            and not retried_without_json_format
        ):
            body = response.text[:800]
            if (
                "text" in body
                or "format" in body
                or "json" in body
                or "unknown" in body
            ):
                retried_without_json_format = True
                payload = _build_openai_responses_payload(
                    model_name=model_name,
                    prompt=prompt,
                    max_output_tokens=max_output_tokens,
                    json_mode=json_mode,
                    include_json_format=False,
                    use_background=use_background,
                )
                print(
                    "[WARN] provider rejected Responses JSON format; retrying with prompt-only JSON instruction"
                )
                continue

        if response.status_code in {429, 500, 502, 503, 504}:
            last_error = RuntimeError(
                f"PROVIDER_HTTP_ERROR: POST {url} status={response.status_code} body={response.text[:500]}"
            )
            if attempt == 3:
                raise last_error
            print(
                f"[WARN] provider HTTP retry on attempt {attempt}/3: status={response.status_code}"
            )
            time.sleep(attempt * 2)
            continue

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(
                f"PROVIDER_HTTP_ERROR: POST {url} status={response.status_code} body={response.text[:500]}"
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            response_text = response.text or ""
            streamed_text = _extract_openai_compatible_text_from_response_body(
                response_text
            )
            if streamed_text:
                _append_llm_usage_event(
                    route="openai_responses",
                    base_url=base_url,
                    request_url=url,
                    model_name=model_name,
                    prompt=prompt,
                    response_text=streamed_text,
                    usage_payload=None,
                )
                return streamed_text
            raise RuntimeError(
                f"PROVIDER_INVALID_JSON: POST {url} returned non-JSON body={response_text[:500]}"
            ) from exc

        extracted_text = _extract_response_text(data)
        if extracted_text:
            _append_llm_usage_event(
                route="openai_responses",
                base_url=base_url,
                request_url=url,
                model_name=model_name,
                prompt=prompt,
                response_text=extracted_text,
                usage_payload=data.get("usage"),
            )
            return extracted_text

        response_id = str(data.get("id", "")).strip()
        status = str(data.get("status", "")).strip().lower()
        if response_id and status in {"queued", "in_progress", "incomplete"}:
            return _poll_openai_response(
                api_key=api_key,
                base_url=base_url,
                response_id=response_id,
                prompt=prompt,
                model_name=model_name,
                timeout_seconds=timeout,
                verify=verify,
            )
        raise RuntimeError(
            f"PROVIDER_EMPTY_OUTPUT: {url} returned no response output. status={status or 'unknown'}"
        )

    if last_error is not None:
        raise last_error
    raise RuntimeError(
        f"PROVIDER_REQUEST_FAILED: POST {url} failed for an unknown reason."
    )


def _build_openai_responses_payload(
    *,
    model_name: str,
    prompt: str,
    max_output_tokens: int,
    json_mode: bool,
    include_json_format: bool,
    use_background: bool,
) -> dict[str, object]:
    final_prompt = prompt
    if json_mode and not include_json_format:
        final_prompt = (
            "Return valid JSON only. Do not include markdown fences or commentary.\n\n"
            + prompt
        )
    payload: dict[str, object] = {
        "model": model_name,
        "input": final_prompt,
        "max_output_tokens": max_output_tokens,
    }
    if include_json_format:
        payload["text"] = {"format": {"type": "json_object"}}
    if _should_use_minimal_reasoning(model_name):
        payload["reasoning"] = {"effort": "minimal"}
    if use_background:
        payload["background"] = True
    return payload


def _poll_openai_response(
    *,
    api_key: str,
    base_url: str,
    response_id: str,
    prompt: str,
    model_name: str,
    timeout_seconds: int,
    verify: bool,
) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Connection": "close",
    }
    url = _openai_response_retrieve_url(base_url, response_id)
    deadline = time.monotonic() + max(30, timeout_seconds)
    sleep_seconds = 2.0
    while time.monotonic() < deadline:
        try:
            response = requests.get(
                url, headers=headers, timeout=min(60, timeout_seconds), verify=verify
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            last_error = RuntimeError(
                f"PROVIDER_REQUEST_FAILED: GET {url} failed: {exc}"
            )
            time.sleep(sleep_seconds)
            sleep_seconds = min(10.0, sleep_seconds * 1.5)
            continue
        except ValueError as exc:
            raise RuntimeError(
                f"PROVIDER_INVALID_JSON: GET {url} returned non-JSON body={response.text[:500]}"
            ) from exc

        extracted_text = _extract_response_text(data)
        if extracted_text:
            _append_llm_usage_event(
                route="openai_responses_poll",
                base_url=base_url,
                request_url=url,
                model_name=model_name,
                prompt=prompt,
                response_text=extracted_text,
                usage_payload=data.get("usage"),
            )
            return extracted_text
        status = str(data.get("status", "")).strip().lower()
        if status in {"failed", "cancelled", "canceled", "expired"}:
            raise RuntimeError(
                f"PROVIDER_RESPONSE_FAILED: GET {url} status={status} body={json.dumps(data)[:500]}"
            )
        last_error = RuntimeError(
            f"PROVIDER_EMPTY_OUTPUT: GET {url} status={status or 'unknown'} returned no output."
        )
        time.sleep(sleep_seconds)
        sleep_seconds = min(10.0, sleep_seconds * 1.5)
    raise last_error


def _request_ollama_native_chat(
    *,
    api_key: str,
    base_url: str,
    model_name: str,
    prompt: str,
    max_output_tokens: int,
    json_mode: bool,
) -> str:
    del api_key
    payload: dict[str, object] = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        "options": {"num_predict": max(1, int(max_output_tokens))},
    }
    if json_mode:
        payload["format"] = "json"
    headers = {"Content-Type": "application/json", "Connection": "close"}
    url = _local_ollama_native_chat_url(base_url)
    retryable_request_errors = (
        requests.exceptions.SSLError,
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    )

    last_error: RuntimeError | None = None
    for attempt in range(1, 4):
        try:
            response = requests.post(
                url,
                headers=headers,
                data=json.dumps(payload),
                timeout=_request_timeout_seconds(base_url=base_url),
            )
        except retryable_request_errors as exc:
            last_error = RuntimeError(
                f"PROVIDER_REQUEST_FAILED: POST {url} failed: {exc}"
            )
            if attempt == 3:
                raise last_error from exc
            print(f"[WARN] provider transport error on attempt {attempt}/3: {exc}")
            time.sleep(attempt * 2)
            continue
        except requests.RequestException as exc:
            raise RuntimeError(
                f"PROVIDER_REQUEST_FAILED: POST {url} failed: {exc}"
            ) from exc

        if response.status_code in {429, 500, 502, 503, 504}:
            last_error = RuntimeError(
                f"PROVIDER_HTTP_ERROR: POST {url} status={response.status_code} body={response.text[:500]}"
            )
            if attempt == 3:
                raise last_error
            print(
                f"[WARN] provider HTTP retry on attempt {attempt}/3: status={response.status_code}"
            )
            time.sleep(attempt * 2)
            continue

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(
                f"PROVIDER_HTTP_ERROR: POST {url} status={response.status_code} body={response.text[:500]}"
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"PROVIDER_INVALID_JSON: POST {url} returned non-JSON body={response.text[:500]}"
            ) from exc

        extracted_text = _extract_ollama_native_chat_text(data)
        if extracted_text:
            _append_llm_usage_event(
                route="ollama_native_chat",
                base_url=base_url,
                request_url=url,
                model_name=model_name,
                prompt=prompt,
                response_text=extracted_text,
                usage_payload={
                    "prompt_tokens": data.get("prompt_eval_count"),
                    "completion_tokens": data.get("eval_count"),
                    "total_tokens": (
                        (
                            int(data.get("prompt_eval_count", 0) or 0)
                            + int(data.get("eval_count", 0) or 0)
                        )
                        if str(data.get("prompt_eval_count", "")).strip()
                        or str(data.get("eval_count", "")).strip()
                        else None
                    ),
                },
            )
            return extracted_text
        error_message = (
            str(data.get("error", "")).strip() if isinstance(data, dict) else ""
        )
        if error_message:
            raise RuntimeError(f"PROVIDER_HTTP_ERROR: POST {url} error={error_message}")
        raise RuntimeError(
            f"PROVIDER_EMPTY_OUTPUT: {url} returned no assistant content."
        )

    if last_error is not None:
        raise last_error
    raise RuntimeError(
        f"PROVIDER_REQUEST_FAILED: POST {url} failed for an unknown reason."
    )


def _should_use_minimal_reasoning(model_name: str) -> bool:
    normalized = model_name.strip().lower()
    return normalized.startswith("gemini-2.5-flash")


def _should_disable_thinking(*, base_url: str, model_name: str) -> bool:
    del model_name
    for key in ("LLM_DISABLE_THINKING", "OLLAMA_DISABLE_THINKING"):
        raw_value = os.getenv(key, "").strip().lower()
        if raw_value in {"1", "true", "yes", "on"}:
            return True
        if raw_value in {"0", "false", "no", "off"}:
            return False
    return _is_local_ollama_base_url(base_url)


def _is_local_ollama_base_url(base_url: str) -> bool:
    try:
        parsed = urlparse(base_url)
    except ValueError:
        return False
    host = (parsed.hostname or "").strip().lower()
    return host in {"127.0.0.1", "localhost", "::1"} and parsed.port == 11434


def _local_ollama_native_chat_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    scheme = parsed.scheme or "http"
    netloc = parsed.netloc or "127.0.0.1:11434"
    return f"{scheme}://{netloc}/api/chat"


def _should_use_openai_responses_api(*, model_name: str) -> bool:
    route = str(os.getenv("LLM_API_ROUTE", "")).strip().lower()
    if route in {"responses", "openai_responses"}:
        return True
    if route in {"chat", "chat_completions", "openai_chat_completions"}:
        return False
    normalized_model = str(model_name or "").strip().lower()
    return bool(re.search(r"(?:^|-)pro(?:-|$)", normalized_model))


def _openai_responses_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/responses"):
        return normalized
    if normalized.endswith("/chat/completions"):
        normalized = normalized[: -len("/chat/completions")]
    if normalized.endswith("/v1"):
        return normalized + "/responses"
    return normalized + "/v1/responses"


def _openai_response_retrieve_url(base_url: str, response_id: str) -> str:
    return _openai_responses_url(base_url).rstrip("/") + "/" + response_id


def _openai_chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    if normalized.endswith("/v1"):
        return normalized + "/chat/completions"
    return normalized + "/v1/chat/completions"


def _extract_response_text(payload: dict[str, object]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    output_array = payload.get("output", []) or []
    for item in output_array:
        if not isinstance(item, dict):
            continue
        # Inspect the content field, which may be a string or an array.
        content_field = item.get("content")
        if isinstance(content_field, str) and content_field.strip():
            return content_field
        # Iterate over array-valued content.
        if isinstance(content_field, list):
            for content in content_field:
                if not isinstance(content, dict):
                    continue
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    return text
                if isinstance(text, dict):
                    text_value = text.get("value")
                    if isinstance(text_value, str) and text_value.strip():
                        return text_value
                output_value = content.get("output_text")
                if isinstance(output_value, str) and output_value.strip():
                    return output_value
    return ""


def _extract_streamed_response_text(events: list[str]) -> str:
    done_text = ""
    delta_parts: list[str] = []
    for event in events:
        if not isinstance(event, str):
            continue
        for line in event.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                payload = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("type") == "response.output_text.done":
                text = payload.get("text")
                if isinstance(text, str) and text.strip():
                    done_text = text
            elif payload.get("type") == "response.output_text.delta":
                delta = payload.get("delta")
                if isinstance(delta, str):
                    delta_parts.append(delta)
            else:
                choices = payload.get("choices")
                if not isinstance(choices, list):
                    continue
                for choice in choices:
                    if not isinstance(choice, dict):
                        continue
                    delta_payload = choice.get("delta")
                    if not isinstance(delta_payload, dict):
                        continue
                    content = delta_payload.get("content")
                    if isinstance(content, str):
                        delta_parts.append(content)
    if done_text:
        return done_text
    return "".join(delta_parts).strip()


def _extract_openai_compatible_text_from_response_body(raw_text: str) -> str:
    text = str(raw_text or "").strip()
    if not text:
        return ""
    if text.startswith("data:"):
        streamed_text = _extract_streamed_response_text([text])
        if streamed_text:
            return streamed_text
    return ""


def _extract_chat_completions_text(payload: dict[str, object]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return ""
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    return text
    return ""


def _extract_ollama_native_chat_text(payload: dict[str, object]) -> str:
    message = payload.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                return text
    return ""


def _request_timeout_seconds(*, base_url: str = "") -> int:
    raw_value = os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "").strip()
    if not raw_value:
        return 300 if _is_local_ollama_base_url(base_url) else 120
    try:
        timeout = int(float(raw_value))
    except ValueError:
        return 300 if _is_local_ollama_base_url(base_url) else 120
    return max(30, timeout)


def _append_llm_usage_event(
    *,
    route: str,
    base_url: str,
    request_url: str,
    model_name: str,
    prompt: str,
    response_text: str,
    usage_payload: object,
) -> None:
    log_path = str(os.getenv("LLM_USAGE_LOG_PATH", "")).strip()
    if not log_path:
        return
    record = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "route": route,
        "base_url": base_url,
        "request_url": request_url,
        "model_name": model_name,
        "prompt_chars": len(prompt),
        "response_chars": len(response_text),
    }
    if isinstance(usage_payload, dict):
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "input_tokens",
            "output_tokens",
        ):
            value = _coerce_optional_int(usage_payload.get(key))
            if value is not None:
                record[key] = value
    try:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[WARN] Unable to append LLM usage log `{log_path}`: {exc}")


def _coerce_optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
