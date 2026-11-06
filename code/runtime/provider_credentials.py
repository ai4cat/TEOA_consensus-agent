"""Read provider credentials from a local pool or the current environment."""

import os
from pathlib import Path


def credentials_available(config):
    pool = config.get('credential_pool')
    return Path(pool).is_file() if pool else bool(os.environ.get('LLM_API_KEY', '').strip())


def load_provider_keys(config):
    pool = config.get('credential_pool')
    if pool:
        from shared.credential_pool import load_encrypted_key_pool
        return load_encrypted_key_pool(Path(pool))
    key = os.environ.get('LLM_API_KEY', '').strip()
    if not key:
        raise ValueError('Set LLM_API_KEY or configure a local credential pool before model approval')
    return [key]
