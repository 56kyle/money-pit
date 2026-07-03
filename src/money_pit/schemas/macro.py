"""MacroIndicators — five-indicator snapshot assembled by the A4 post-processor for regime classification."""
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict


class MacroIndicators(BaseModel):
    """Five economic indicator readings consumed by compute/regime.classify_regime().

    Any field left None triggers UNCERTAIN in the regime truth table.
    Assembled inline from macro_regime answers in initial_answers.json; not written to disk.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    yield_curve: float | None
    credit_spreads: float | None
    pmi: float | None
    earnings_revisions: float | None
    inflation: float | None
    as_of: str | None
