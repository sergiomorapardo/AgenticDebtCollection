"""Enruta únicamente decisiones ya validadas por el nodo anterior."""

from .state import Destination, RouteState


def route_after_confirmation(state: RouteState) -> Destination:
    destination = state["confirmation_route"]
    if destination not in ("commitment", "response"):
        raise ValueError(f"Destino inválido: {destination}")
    return destination
