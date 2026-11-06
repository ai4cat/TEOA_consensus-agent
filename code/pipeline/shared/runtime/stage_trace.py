"""Immutable stage records and conservative artifact change impact.

The input set is a configuration snapshot, not a claim of exact file reads.
Scientific dependency edges must still be supplied by domain validators.
"""

from contextvars import ContextVar


ACTIVE_STAGE = ContextVar("entity_stage_trace", default=None)


