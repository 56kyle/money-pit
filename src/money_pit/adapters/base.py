"""Module containing the SourceAdapter ABC used throughout the money_pit package."""
from abc import ABC
from abc import abstractmethod
from typing import Generic
from typing import TypeVar

from money_pit.schemas.signals import SignalSet


Payload = TypeVar("Payload")


class SourceAdapter(ABC, Generic[Payload]):
    """Abstract base class every source adapter must subclass."""

    @abstractmethod
    def process(self, payload: Payload) -> SignalSet:
        ...
