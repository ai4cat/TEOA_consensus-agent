from __future__ import annotations

import os
from pathlib import Path
from dotenv import dotenv_values


def require_llm_env(llm_role: str | None = None) -> dict[str, str]:
    resolved = load_llm_env_if_available(llm_role=llm_role)
    missing = [key for key, value in resolved.items() if not value]
    if missing:
        role_label = f" for {str(llm_role).upper()}" if llm_role else ""
        raise RuntimeError(
            f"Missing required LLM configuration{role_label}: {', '.join(missing)}"
        )
    return resolved


def load_llm_env_if_available(llm_role: str | None = None) -> dict[str, str]:
    dotenv_value_maps = _load_candidate_dotenvs()
    return {
        "LLM_API_KEY": _resolve_llm_env_value(
            "LLM_API_KEY", llm_role=llm_role, dotenv_value_maps=dotenv_value_maps
        ),
        "LLM_API_BASE_URL": _resolve_llm_env_value(
            "LLM_API_BASE_URL",
            llm_role=llm_role,
            dotenv_value_maps=dotenv_value_maps,
        ),
        "LLM_MODEL_NAME": _resolve_llm_env_value(
            "LLM_MODEL_NAME",
            llm_role=llm_role,
            dotenv_value_maps=dotenv_value_maps,
        ),
    }


def _load_candidate_dotenvs() -> list[dict[str, str]]:
    dotenv_value_maps: list[dict[str, str]] = []
    for dotenv_path in _candidate_dotenv_paths():
        values = dotenv_values(dotenv_path)
        normalized_values = {
            str(key): str(value) for key, value in values.items() if value is not None
        }
        dotenv_value_maps.append(normalized_values)
    return dotenv_value_maps


def _resolve_llm_env_value(
    base_key: str,
    *,
    llm_role: str | None,
    dotenv_value_maps: list[dict[str, str]],
) -> str:
    role_prefix = str(llm_role or "").strip().upper()
    generic_env_value = os.getenv(base_key)
    if role_prefix:
        role_key = f"{role_prefix}_{base_key}"
        role_env_value = os.getenv(role_key)
        if role_env_value:
            return role_env_value
        if generic_env_value:
            return generic_env_value
        for value_map in dotenv_value_maps:
            candidate = str(value_map.get(role_key, "")).strip()
            if candidate:
                return candidate

    if generic_env_value:
        return generic_env_value
    for value_map in dotenv_value_maps:
        candidate = str(value_map.get(base_key, "")).strip()
        if candidate:
            return candidate
    return ""


def _candidate_dotenv_paths() -> list[Path]:
    current = Path.cwd().resolve()
    candidates: list[Path] = []
    seen: set[Path] = set()
    for directory in (current, *current.parents):
        path = directory / ".env"
        if path in seen:
            continue
        seen.add(path)
        if path.exists():
            candidates.append(path)
    return candidates
