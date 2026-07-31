"""Module containing layered investment-universe construction."""

from collections.abc import Iterable
from enum import StrEnum
from typing import ClassVar
from typing import Self

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator


class UniverseLayer(StrEnum):
    """Origins from which an investment candidate can enter the universe."""

    HOLDING = "holding"
    WATCHLIST = "watchlist"
    SOURCE_MENTION = "source_mention"
    BENCHMARK = "benchmark"
    QUANTITATIVE_SCREEN = "quantitative_screen"
    EXPLICIT_PROXY = "explicit_proxy"


class CandidateReference(BaseModel):
    """One resolved or unresolved reference supplied by a universe layer."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    reference: str = Field(min_length=1)
    instrument: str | None
    tradable: bool | None
    observation_reason: str | None
    proxy_for: str | None

    @model_validator(mode="after")
    def validate_resolution(self) -> Self:
        """Require explicit resolution state and proxy provenance."""
        if self.instrument is None:
            if self.tradable is not None:
                raise ValueError("an unresolved reference cannot declare tradability")
            if self.observation_reason is None:
                raise ValueError("an unresolved reference requires an observation reason")
        elif self.tradable is None:
            raise ValueError("a resolved instrument requires an explicit tradability decision")
        elif not self.tradable and self.observation_reason is None:
            raise ValueError("a non-tradable instrument requires an observation reason")
        if self.proxy_for is not None and self.instrument is None:
            raise ValueError("a proxy must name its explicit tradable instrument")
        return self


class UniverseCandidate(BaseModel):
    """A tradable candidate with every contributing layer preserved."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    instrument: str = Field(min_length=1)
    layers: tuple[UniverseLayer, ...] = Field(min_length=1)
    references: tuple[str, ...] = Field(min_length=1)
    proxy_for: tuple[str, ...] = ()


class ObservationOnlyCandidate(BaseModel):
    """An ambiguous or non-tradable reference retained without capital authority."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    reference: str = Field(min_length=1)
    layer: UniverseLayer
    instrument: str | None
    reason: str = Field(min_length=1)


class LayeredUniverse(BaseModel):
    """The deterministic union of tradable and observation-only candidates."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    candidates: tuple[UniverseCandidate, ...]
    observation_only: tuple[ObservationOnlyCandidate, ...]

    @property
    def instruments(self) -> tuple[str, ...]:
        """Return tradable instruments in canonical order."""
        return tuple(candidate.instrument for candidate in self.candidates)


def _canonical_instrument(instrument: str) -> str:
    canonical: str = instrument.strip().upper()
    if not canonical:
        raise ValueError("instrument cannot be blank")
    return canonical


def build_layered_universe(
    *,
    holdings: Iterable[CandidateReference] = (),
    watchlist: Iterable[CandidateReference] = (),
    source_mentions: Iterable[CandidateReference] = (),
    benchmark_constituents: Iterable[CandidateReference] = (),
    quantitative_screens: Iterable[CandidateReference] = (),
    explicit_proxies: Iterable[CandidateReference] = (),
) -> LayeredUniverse:
    """Union universe layers without silently resolving ambiguity or proxies."""
    layered_references: tuple[tuple[UniverseLayer, Iterable[CandidateReference]], ...] = (
        (UniverseLayer.HOLDING, holdings),
        (UniverseLayer.WATCHLIST, watchlist),
        (UniverseLayer.SOURCE_MENTION, source_mentions),
        (UniverseLayer.BENCHMARK, benchmark_constituents),
        (UniverseLayer.QUANTITATIVE_SCREEN, quantitative_screens),
        (UniverseLayer.EXPLICIT_PROXY, explicit_proxies),
    )
    candidate_layers: dict[str, set[UniverseLayer]] = {}
    candidate_references: dict[str, set[str]] = {}
    candidate_proxies: dict[str, set[str]] = {}
    restricted_instruments: set[str] = set()
    observation_only: list[ObservationOnlyCandidate] = []

    for layer, references in layered_references:
        for reference in references:
            if reference.instrument is None or reference.tradable is not True:
                if reference.instrument is not None:
                    restricted_instruments.add(_canonical_instrument(reference.instrument))
                observation_only.append(
                    ObservationOnlyCandidate(
                        reference=reference.reference,
                        layer=layer,
                        instrument=(
                            None if reference.instrument is None else _canonical_instrument(reference.instrument)
                        ),
                        reason=reference.observation_reason or "instrument is not approved as tradable",
                    )
                )
                continue
            instrument: str = _canonical_instrument(reference.instrument)
            candidate_layers.setdefault(instrument, set()).add(layer)
            candidate_references.setdefault(instrument, set()).add(reference.reference)
            if reference.proxy_for is not None:
                candidate_proxies.setdefault(instrument, set()).add(reference.proxy_for)

    candidates: tuple[UniverseCandidate, ...] = tuple(
        UniverseCandidate(
            instrument=instrument,
            layers=tuple(sorted(candidate_layers[instrument], key=str)),
            references=tuple(sorted(candidate_references[instrument])),
            proxy_for=tuple(sorted(candidate_proxies.get(instrument, set()))),
        )
        for instrument in sorted(candidate_layers)
        if instrument not in restricted_instruments
    )
    observations: tuple[ObservationOnlyCandidate, ...] = tuple(
        sorted(
            observation_only,
            key=lambda candidate: (
                candidate.reference,
                candidate.layer.value,
                candidate.instrument or "",
            ),
        )
    )
    return LayeredUniverse(candidates=candidates, observation_only=observations)
