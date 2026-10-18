"""Datos mínimos que requiere esta decisión."""

from typing import Literal

from typing_extensions import TypedDict

Destination = Literal["promise", "negotiation", "confirmation", "handoff", "close", "response"]


class RouteState(TypedDict):
    intent_route: Destination
