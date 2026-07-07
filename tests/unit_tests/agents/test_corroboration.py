"""Tests for money_pit.agents.corroboration."""

from money_pit.agents.corroboration import corroborate
from money_pit.schemas.aggregation_draft import ClaimRelations


def test_corroborate_reports_no_relations() -> None:
    assert corroborate([]) == ClaimRelations(agree=[], disagree=[])
