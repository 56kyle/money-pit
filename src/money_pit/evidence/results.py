"""Typed evidence-processing results shared by every processor."""

from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.schemas.evidence import EvidenceDocument


class DerivedEvidenceDocument(BaseModel):
    """One immutable derived asset and its evidence document."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    content: bytes
    document: EvidenceDocument


class EvidenceProcessingBundle(BaseModel):
    """Primary acquisition evidence plus immutable derived evidence assets."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    primary: EvidenceDocument
    derived: tuple[DerivedEvidenceDocument, ...] = ()
