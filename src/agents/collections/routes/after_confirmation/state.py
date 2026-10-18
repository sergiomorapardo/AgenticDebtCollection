"""Datos mínimos que requiere esta decisión."""

from typing import Literal

from typing_extensions import TypedDict

Destination = Literal["commitment", "response"]


class RouteState(TypedDict):
    confirmation_route: Destination
