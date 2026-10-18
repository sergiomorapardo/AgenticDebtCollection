"""Datos mínimos que requiere esta decisión."""

from typing import Literal

from typing_extensions import TypedDict

Destination = Literal["interpret", "response", "close"]


class RouteState(TypedDict):
    intake_route: Destination
