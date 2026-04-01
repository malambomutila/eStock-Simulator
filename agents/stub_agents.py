"""
Placeholder personas until Eng 3 / Eng 4 ship real agents.

Return stable, explicit failures so the orchestrator and UI never crash on
missing implementations.
"""

from __future__ import annotations

from agents.base_agent import BaseAgent
from core.models import ActionType, AgentRole
from core.schemas import AgentMessage, AgentResponse


class PlaceholderDoctorAgent(BaseAgent):
    role = AgentRole.DOCTOR

    async def handle(self, message: AgentMessage) -> AgentResponse:
        return self._fail(
            message,
            ActionType.SYSTEM_ERROR,
            "PlaceholderDoctorAgent — use agents.doctor_agent.DoctorAgent (default in build_default_orchestrator) or SimulationDoctorAgent for tests.",
        )


class PlaceholderStockManagerAgent(BaseAgent):
    role = AgentRole.STOCK_MANAGER

    async def handle(self, message: AgentMessage) -> AgentResponse:
        return self._fail(
            message,
            ActionType.SYSTEM_ERROR,
            "Eng 4: StockManagerAgent not implemented.",
        )


class PlaceholderGovAgent(BaseAgent):
    role = AgentRole.GOV_OFFICIAL

    async def handle(self, message: AgentMessage) -> AgentResponse:
        return self._fail(
            message,
            ActionType.SYSTEM_ERROR,
            "PlaceholderGovAgent — use agents.gov_official_agent.GovOfficialAgent (default in build_default_orchestrator).",
        )
