"""Solicita la escritura; response la confirma junto al turno en una transacción."""

from agents.collections.state import State


def save_commitment(state: State) -> dict:
    if (
        not state.get("confirmed_offer")
        or state["confirmed_offer"]["offer_id"] != state["session"]["pending_offer_id"]
    ):
        raise ValueError("No existe una oferta revalidada para registrar.")
    return {"commit_requested": True}
