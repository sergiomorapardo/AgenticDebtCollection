"""Entrada pública de simulación; no admite atributos de identidad o saldo."""

from pydantic import BaseModel, ConfigDict, Field


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    event_id: str = Field(min_length=1, max_length=100)
    account_id: str = "demo-001"
    text: str = Field(default="", max_length=4000)
