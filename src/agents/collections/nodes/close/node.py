"""Finaliza la sesión a petición del usuario o por control del flujo."""

from agents.collections.state import State


def close_session(state: State) -> dict:
    session = dict(state["session"])
    session.update(stage="closed", pending_offer_id=None, close_reason="customer_request")
    return {
        "session": session,
        "response_text": "Entendido, cierro la conversación. No se ha registrado un nuevo compromiso ni realizado un pago.",
    }
