"""Entrada pública mínima y estado interno recuperado desde el repositorio."""

from typing import Literal

from typing_extensions import TypedDict


class InputState(TypedDict):
    session_id: str
    event_id: str
    text: str
    account_id: str


class State(InputState, total=False):
    semantic_result: dict | None
    session: dict
    interpretation: dict
    intake_route: Literal["interpret", "response", "close"]
    intent_route: Literal["promise", "negotiation", "confirmation", "handoff", "close", "response"]
    confirmation_route: Literal["commitment", "response"]
    response_text: str
    reply: dict
    replay: bool
    input_hash: str
    draft_offer: dict | None
    confirmed_offer: dict | None

    commit_requested: bool

    streamed_reply: dict | None
    wording_reference: str
    wording_draft: str
    wording_attempts: int
    wording_status: str
    wording_route: Literal["draft_reply", "response"]
