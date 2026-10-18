"""Enruta únicamente decisiones ya validadas por el nodo anterior."""

from .state import Destination, RouteState


def route_after_intake(state: RouteState) -> Destination:
    destination = state["intake_route"]
    if destination not in ("interpret", "response", "close"):
        raise ValueError(f"Destino inválido: {destination}")
    return destination
