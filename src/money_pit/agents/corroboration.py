"""Corroboration agent stub for single-source runs. Real LLM+embedding implementation activates when N>1 source adapters land and can be tested end-to-end."""

from money_pit.schemas.aggregation_draft import ClaimRelations
from money_pit.schemas.signals import Claim


def corroborate(claims: list[Claim]) -> ClaimRelations:
    _ = claims
    return ClaimRelations(agree=[], disagree=[])
