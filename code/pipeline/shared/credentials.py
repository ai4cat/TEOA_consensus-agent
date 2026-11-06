"""Read an explicitly selected credential file without saving its secret."""

import re
from pathlib import Path
from urllib.parse import urlsplit


def credentials(path):
    raw = Path(path).read_text(encoding="utf-8-sig").strip()
    keys = re.findall(r"(?<![\w-])sk-[A-Za-z0-9_-]+", raw)
    urls = re.findall(r"https://[^\s\"\'<>]+", raw)
    if len(set(keys)) != 1 or len(set(urls)) > 1:
        raise ValueError("Expected exactly one key and at most one HTTPS endpoint")
    if not urls:
        raise ValueError("No endpoint in credential file; do not guess a recipient")
    endpoint = urls[0].rstrip("/,;")
    parsed = urlsplit(endpoint)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Endpoint must not contain credentials, query or fragment")
    base = endpoint if parsed.path.rstrip("/").endswith("/v1") else endpoint + "/v1"
    return keys[0], base
