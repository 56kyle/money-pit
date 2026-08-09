"""Module applying deterministic temporal compatibility before signal synthesis."""

from collections.abc import Mapping
from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.theses import ThesisRevision


_HORIZON_ORDER: dict[HorizonClass, int] = {
    HorizonClass.EVENT: 0,
    HorizonClass.TACTICAL: 1,
    HorizonClass.MEDIUM_TERM: 2,
    HorizonClass.STRUCTURAL: 3,
}


class TemporalCompatibility(BaseModel):
    """Deterministic eligibility result for one observation/revision pair."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    compatible: bool
    reasons: tuple[str, ...]


class HistoricalModelUnavailableError(Exception):
    """Raised before an uncertified model can add hindsight to a historical run."""


def require_model_temporal_authority(
    *,
    requested_as_of: datetime,
    run_started_at: datetime,
    requested_as_of_explicit: bool,
    point_in_time_certified: bool,
) -> None:
    """Fail closed when an explicit historical run would invoke a current model."""
    if requested_as_of_explicit and requested_as_of < run_started_at and not point_in_time_certified:
        raise HistoricalModelUnavailableError(
            "Explicit historical runs require a point-in-time-certified model or replayed outputs",
        )


def temporal_compatibility(
    observation: ClaimObservation,
    revision: ThesisRevision,
    *,
    as_of: datetime,
    bridge_authorized: bool,
    explicit_update: bool,
    proxy_bridge_authorized: bool = False,
    proxy_relationships: Mapping[str, frozenset[str]] | None = None,
) -> TemporalCompatibility:
    """Decide whether one observation may contribute to one thesis revision."""
    reasons: list[str] = []
    compatible: bool = True
    if observation.known_at > as_of or revision.known_at > as_of:
        compatible = False
        reasons.append("not_known_as_of")

    observation_instruments: set[str] = {instrument.upper() for instrument in observation.instruments}
    revision_instruments: set[str] = {revision.instrument.upper()} if revision.instrument is not None else set()
    normalized_themes = {theme.strip().casefold() for theme in observation.themes}
    revision_theme: set[str] = {revision.theme.strip().casefold()} if revision.theme is not None else set()
    observation_subjects: set[str] = {*observation_instruments, *normalized_themes}
    configured_proxies = {
        subject.strip().casefold(): frozenset(proxy.upper() for proxy in proxies)
        for subject, proxies in (proxy_relationships or {}).items()
    }
    proxy_matches: bool = any(
        revision_instruments & configured_proxies.get(subject.strip().casefold(), frozenset())
        for subject in observation_subjects
    )
    subject_matches: bool = bool(
        (observation_instruments & revision_instruments)
        or (normalized_themes & revision_theme)
        or proxy_matches
        or proxy_bridge_authorized
    )
    if not subject_matches:
        compatible = False
        reasons.append("subject_mismatch")

    horizon_distance: int = abs(
        _HORIZON_ORDER[observation.horizon_class] - _HORIZON_ORDER[revision.horizon_class],
    )
    if horizon_distance > 1 and not bridge_authorized:
        compatible = False
        reasons.append("nonadjacent_horizons_without_bridge")

    observation_start: datetime = observation.effective_from or observation.asserted_at
    observation_end: datetime | None = observation.valid_until
    revision_start: datetime = revision.effective_from
    revision_end: datetime | None = revision.valid_until
    intervals_overlap: bool = not (
        (observation_end is not None and observation_end < revision_start)
        or (revision_end is not None and revision_end < observation_start)
    )
    if not intervals_overlap and not explicit_update:
        compatible = False
        reasons.append("economic_intervals_do_not_overlap")

    mechanisms_overlap: bool = bool(
        set(observation.causal_mechanisms) & set(revision.causal_mechanisms),
    )
    regimes_overlap: bool = bool(set(observation.regime_assumptions) & set(revision.regime_assumptions))
    if (
        observation.causal_mechanisms
        and revision.causal_mechanisms
        and not mechanisms_overlap
        and not bridge_authorized
    ):
        compatible = False
        reasons.append("causal_mechanisms_do_not_overlap")
    if observation.regime_assumptions and revision.regime_assumptions and not regimes_overlap and not bridge_authorized:
        compatible = False
        reasons.append("regime_assumptions_do_not_overlap")

    if compatible:
        reasons.append("deterministic_compatibility_passed")
    return TemporalCompatibility(compatible=compatible, reasons=tuple(reasons))
