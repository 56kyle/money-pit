"""Module containing the cross-layer dependency and contract aliases shared by the agents, pipeline, and graph layers of the money_pit package.

Depends only on money_pit.schemas so it stays a cycle-free leaf importable by every layer
that produces or consumes these callables.
"""

from collections.abc import Callable
from collections.abc import Mapping
from typing import TYPE_CHECKING
from typing import TypeAlias

from money_pit.schemas.action_steps import ExecutionParameters
from money_pit.schemas.aggregation_draft import ClaimRelations
from money_pit.schemas.analysis_draft import AnalysisJudgment
from money_pit.schemas.answer_draft import AnswerDraft
from money_pit.schemas.answers import InitialAnswers
from money_pit.schemas.fills import FillObservation
from money_pit.schemas.portfolio import PortfolioSnapshot
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.question_draft import DraftQuestion
from money_pit.schemas.questions import Question
from money_pit.schemas.signal_draft import SignalSetDraft
from money_pit.schemas.signals import AggregatedSignals
from money_pit.schemas.signals import Claim


if TYPE_CHECKING:
    from money_pit.adapters.video_llm import VideoPayload


ToolManifest: TypeAlias = Mapping[str, dict[str, object]]
VideoAgent: TypeAlias = Callable[["VideoPayload"], SignalSetDraft]
CorroborationAgent: TypeAlias = Callable[[list[Claim]], ClaimRelations]
ClaimQuestionsAgent: TypeAlias = Callable[[list[Claim]], list[DraftQuestion]]
AnswerSynthesisAgent: TypeAlias = Callable[[list[Question], list[SourceRef]], list[AnswerDraft]]
ThesisAgent: TypeAlias = Callable[[AggregatedSignals, PortfolioSnapshot, InitialAnswers], AnalysisJudgment]
PortfolioFetcher: TypeAlias = Callable[[str], PortfolioSnapshot]
OrderPlacer: TypeAlias = Callable[[ExecutionParameters], str]
FillObserver: TypeAlias = Callable[[str], FillObservation]
EmailSender: TypeAlias = Callable[[str, str], None]
