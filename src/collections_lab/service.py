"""Servicio local: un turno y sus escrituras forman una transacción."""

from agents.collections.agent import agent
from agents.collections.runtime import AppContext, default_context
from collections_lab.channels.contracts import TurnRequest


class ConversationService:
    def __init__(self, context: AppContext | None = None):
        self.context = context or default_context()

    def respond(self, request: TurnRequest) -> dict:
        # Serialización deliberada para la demo SQLite: una escritura no sobrevive
        # a un turno fallido. Staging necesitará concurrencia por sesión y autenticación.
        with self.context.repository.connection():
            result = agent.invoke(request.model_dump(), context=self.context)
        return result["reply"]
