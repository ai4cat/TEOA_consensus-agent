"""Identifiers distinguish model-assigned groups without inferring equivalence."""

from uuid import uuid4


def new_claim_id() -> str:
    return "C_" + uuid4().hex
