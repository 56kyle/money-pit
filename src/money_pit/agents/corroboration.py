"""Module containing the corroboration agent stub for single-source runs in the money_pit package.

The real LLM+embedding implementation activates when N>1 source adapters land and can be tested end-to-end.
"""

from money_pit.schemas.aggregation_draft import ClaimRelations
from money_pit.schemas.signals import Claim


def corroborate(claims: list[Claim]) -> ClaimRelations:
    """Deferred corroboration stub: performs no corroboration, always reports no relations.

    At N=1 source there is nothing to corroborate, so returning empty agree/disagree
    relations is correct today. The real LLM+embedding implementation activates when
    N>1 source adapters land; until then this is a no-op and needs a guard test that
    turns red the moment multi-source input reaches it.
    """
    _ = claims
    return ClaimRelations(agree=[], disagree=[])
