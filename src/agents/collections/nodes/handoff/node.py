"""Derivación simulada, sin contactar a ninguna persona."""

from agents.collections.state import State


def prepare_handoff(state: State) -> dict:
    session = dict(state["session"])
    session.update(stage="handoff", pending_offer_id=None)
    reason = state.get("interpretation", {}).get("intent", "unclear")
    session["handoff_reason"] = reason
    if reason == "reported_payment":
        text = "Dejo registrado que reportaste un pago para revisión. Aún no he verificado ese pago ni cambiado el saldo."
    elif reason == "request_exception":
        text = "No puedo aprobar descuentos automáticos. Registro tu solicitud para revisión, sin aprobar condiciones. No puedo transferirte a un asesor desde aquí."
    elif reason == "dispute":
        text = "Registro que no reconoces la obligación y una solicitud de revisión. Finalizo la negociación de esta sesión."
    else:
        text = "Registro una solicitud de atención de un asesor y finalizo esta sesión. No puedo transferirte a un asesor desde aquí."
    return {"session": session, "response_text": text}
