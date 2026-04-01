"""
Abstract base for all agent personas (Eng 2).

Subclasses implement ``handle()`` and set ``role``. Helpers build
``AgentResponse`` and optional ``AuditLogCreate`` rows consistently.
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from datetime import datetime

from core.models import ActionType, AgentRole
from core.schemas import AgentMessage, AgentResponse, AuditLogCreate


class BaseAgent(ABC):
    """
    Abstract base for all agent personas.

    Subclasses must implement `handle(message)` and declare their `role`.
    """

    role: AgentRole  # must be set on each subclass

    def __init__(self, agent_id: str | None = None):
        self.agent_id = agent_id or f"{self.__class__.__name__}-{uuid.uuid4().hex[:8]}"
        self.logger = logging.getLogger(f"agent.{self.agent_id}")

    @abstractmethod
    async def handle(self, message: AgentMessage) -> AgentResponse:
        """Process an incoming orchestrator message and return a response."""
        ...

    def _make_audit_entry(
        self,
        message: AgentMessage,
        action_type: ActionType,
        entity_type: str | None,
        entity_id: str | None,
        payload: dict,
        is_anomaly: bool = False,
        anomaly_reason: str | None = None,
    ) -> AuditLogCreate:
        return AuditLogCreate(
            facility_id=message.facility_id,
            agent_id=self.agent_id,
            agent_role=self.role,
            action_type=action_type,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload,
            is_anomaly=is_anomaly,
            anomaly_reason=anomaly_reason,
            correlation_id=message.correlation_id,
        )

    def _ok(
        self,
        message: AgentMessage,
        action_type: ActionType,
        result: dict,
        audit_entry: AuditLogCreate | None = None,
    ) -> AgentResponse:
        return AgentResponse(
            message_id=message.message_id,
            correlation_id=message.correlation_id,
            success=True,
            action_type=action_type,
            agent_id=self.agent_id,
            agent_role=self.role,
            result=result,
            audit_entry=audit_entry,
        )

    def _fail(
        self,
        message: AgentMessage,
        action_type: ActionType,
        error: str,
        audit_entry: AuditLogCreate | None = None,
    ) -> AgentResponse:
        self.logger.warning("Action failed [%s]: %s", action_type, error)
        return AgentResponse(
            message_id=message.message_id,
            correlation_id=message.correlation_id,
            success=False,
            action_type=action_type,
            agent_id=self.agent_id,
            agent_role=self.role,
            error=error,
            audit_entry=audit_entry,
        )
