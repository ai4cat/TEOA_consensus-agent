"""Shared MinerU page-index parsing; preserves strict boolean rejection."""


def _parse_page_idx(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("-"):
            stripped = stripped[1:]
            if stripped.isdigit():
                return -int(stripped)
            return None
        if stripped.isdigit():
            return int(stripped)
    return None
