"""Module containing deterministic candidate-to-observation grounding."""

from money_pit.schemas.claims import CanonicalClaim
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.theses import CandidateThesis


CANDIDATE_GROUNDING_POLICY_VERSION = "candidate-grounding-v1"


class CandidateGroundingError(Exception):
    """Raised when source-caused candidate work has no exact observation grounding."""


def candidate_grounding_observation_ids(
    candidate: CandidateThesis,
    *,
    observations: tuple[ClaimObservation, ...],
    claims: tuple[CanonicalClaim, ...],
    eligible_observation_ids: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """Return exact immutable observations supporting one candidate."""
    eligible = (
        {item.observation_id for item in observations}
        if eligible_observation_ids is None
        else set(eligible_observation_ids)
    )
    basis = candidate.discovery_basis
    if basis.source_claim_keys:
        active_ids = {
            observation_id
            for claim in claims
            if claim.canonical_claim_key in basis.source_claim_keys
            for observation_id in claim.active_observation_ids
        }
        return tuple(
            item.observation_id
            for item in observations
            if item.observation_id in eligible and item.observation_id in active_ids
        )

    reference = basis.universe_reference
    if reference is None:
        return ()
    normalized_reference = _normalized_reference(reference)
    return tuple(
        item.observation_id
        for item in observations
        if item.observation_id in eligible
        and normalized_reference
        in {
            _normalized_reference(value)
            for value in (*item.instruments, *item.themes)
        }
    )


def require_candidate_grounding(
    candidate: CandidateThesis,
    *,
    observations: tuple[ClaimObservation, ...],
    claims: tuple[CanonicalClaim, ...],
    eligible_observation_ids: frozenset[str],
) -> tuple[str, ...]:
    """Require candidate-specific grounding when its discovery unit carried observations."""
    grounded = candidate_grounding_observation_ids(
        candidate,
        observations=observations,
        claims=claims,
        eligible_observation_ids=eligible_observation_ids,
    )
    if eligible_observation_ids and not grounded:
        raise CandidateGroundingError(
            f"Candidate has no exact observation grounding in its discovery work: {candidate.candidate_thesis_id}"
        )
    return grounded


def _normalized_reference(reference: str) -> str:
    return " ".join(reference.split()).casefold()
