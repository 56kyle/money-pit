"""Module containing the ordered persistent intelligence stages."""

from enum import StrEnum


class Stage(StrEnum):
    """One bounded stage in the persistent investment-research harness."""

    A1 = "A1"
    A2 = "A2"
    A3 = "A3"
    A4 = "A4"
    A5 = "A5"
    A6 = "A6"


PLANNING_CHAIN: tuple[Stage, ...] = tuple(Stage)


def interrupt_before_successor_of(through: Stage | None) -> tuple[str, ...]:
    """Return the next node at which a bounded run must pause.

    A6 already ends the harness, so requesting ``--through A6`` is equivalent to an
    unbounded run and needs no interrupt.
    """
    if through is None or through is Stage.A6:
        return ()
    successor_index: int = PLANNING_CHAIN.index(through) + 1
    return (PLANNING_CHAIN[successor_index].value,)
