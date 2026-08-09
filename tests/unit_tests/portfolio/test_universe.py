"""Tests for layered investment-universe construction."""

import pytest
from pydantic import ValidationError

from money_pit.portfolio.universe import CandidateReference
from money_pit.portfolio.universe import (
    _canonical_instrument,  # pyright: ignore[reportPrivateUsage]  # Contract test pins canonical symbol normalization.
)
from money_pit.portfolio.universe import build_layered_universe
from money_pit.schemas.universe import UniverseLayer


def _tradable(reference: str, instrument: str, *, proxy_for: str | None = None) -> CandidateReference:
    return CandidateReference(
        reference=reference,
        instrument=instrument,
        tradable=True,
        observation_reason=None,
        proxy_for=proxy_for,
    )


def test__canonical_instrument_normalizes_symbol() -> None:
    assert _canonical_instrument(" spy ") == "SPY"


def test__canonical_instrument_with_blank_rejects_value() -> None:
    with pytest.raises(ValueError, match="instrument cannot be blank"):
        _ = _canonical_instrument(" ")


def test_build_layered_universe_preserves_all_contributing_layers() -> None:
    universe = build_layered_universe(
        holdings=(_tradable("held SPY", "spy"),),
        benchmark_constituents=(_tradable("S&P 500 constituent", "SPY"),),
    )

    assert universe.candidates[0].layers == (UniverseLayer.BENCHMARK, UniverseLayer.HOLDING)


def test_build_layered_universe_does_not_silently_substitute_proxy() -> None:
    universe = build_layered_universe(
        source_mentions=(
            CandidateReference(
                reference="Foreign Co.",
                instrument=None,
                tradable=None,
                observation_reason="ambiguous foreign listing",
                proxy_for=None,
            ),
        )
    )

    assert universe.instruments == ()


def test_build_layered_universe_preserves_explicit_proxy_provenance() -> None:
    universe = build_layered_universe(
        explicit_proxies=(_tradable("Semiconductor basket", "SMH", proxy_for="Foreign Chip Co."),)
    )

    assert universe.candidates[0].proxy_for == ("Foreign Chip Co.",)


def test_candidate_reference_with_unresolved_tradability_rejects_value() -> None:
    with pytest.raises(ValidationError):
        _ = CandidateReference(
            reference="Ambiguous Co.",
            instrument=None,
            tradable=True,
            observation_reason="ambiguous",
            proxy_for=None,
        )
