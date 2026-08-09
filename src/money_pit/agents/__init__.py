"""Subpackage containing capability-scoped A1-A4 model agents."""

from money_pit.agents.discovery import make_discovery_agent
from money_pit.agents.interpretation import make_interpretation_agent
from money_pit.agents.research_planner import make_research_planning_agent
from money_pit.agents.synthesis import make_synthesis_agent


__all__ = [
    "make_discovery_agent",
    "make_interpretation_agent",
    "make_research_planning_agent",
    "make_synthesis_agent",
]
