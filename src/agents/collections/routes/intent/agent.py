"""Enruta únicamente decisiones ya validadas por el nodo anterior."""

from .state import Destination, RouteState


def route_intent(state: RouteState) -> Destination:
    destination = state["intent_route"]
    if destination not in (
        "promise",
        "negotiation",
        "confirmation",
        "handoff",
        "close",
        "response",
    ):
        raise ValueError(f"Destino inválido: {destination}")
    return destination
