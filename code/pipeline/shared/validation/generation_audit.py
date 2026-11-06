"""Immutable per-attempt C/H/E audit, separate from scientific acceptance.

Raw text is saved before parsing. Validators receive a copy; a failed attempt
never creates validated_output.json. There is no implicit citation repair.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_ACTIVE = ContextVar("entity_generation_audit", default=None)


@contextmanager
def audit_scope(root, run_id):
    token = _ACTIVE.set((Path(root), str(run_id)))
    try:
        yield
    finally:
        _ACTIVE.reset(token)


