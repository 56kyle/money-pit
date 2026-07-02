"""Module containing the injected-dependency type aliases used across the money_pit pipeline."""
from collections.abc import Callable
from collections.abc import Mapping
from typing import TypeAlias

from money_pit.schemas.aggregation_draft import ClaimRelations
from money_pit.schemas.analysis_draft import AnalysisJudgment
from money_pit.schemas.answer_draft import AnswerDraft
from money_pit.schemas.answers import InitialAnswers
from money_pit.schemas.portfolio import PortfolioSnapshot
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.question_draft import DraftQuestion
from money_pit.schemas.questions import Question
from money_pit.schemas.signals import AggregatedSignals
from money_pit.schemas.signals import Claim


ToolManifest: TypeAlias = Mapping[str, dict[str, object]]
CorroborationAgent: TypeAlias = Callable[[list[Claim]], ClaimRelations]
ClaimQuestionsAgent: TypeAlias = Callable[[list[Claim]], list[DraftQuestion]]
AnswerSynthesisAgent: TypeAlias = Callable[[list[Question], list[SourceRef]], list[AnswerDraft]]
ThesisAgent: TypeAlias = Callable[[AggregatedSignals, PortfolioSnapshot, InitialAnswers], AnalysisJudgment]
