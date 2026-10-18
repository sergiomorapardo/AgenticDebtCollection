"""Derivación simulada, sin contactar a ninguna persona."""

from agents.collections.state import State


def prepare_handoff(state: State) -> dict:
    session = dict(state["session"])
    session.update(stage="handoff", pending_offer_id=None)
    reason = state.get("interpretation", {}).get("intent", "unclear")
    session["handoff_reason"] = reason
    if reason == "reported_payment":
        text = "Dejo registrado que reportaste un pago para revisión humana simulada. Aún no he verificado ese pago ni cambiado el saldo."
    elif reason == "request_exception":
        text = "La política ficticia no permite descuentos automáticos. Registro tu solicitud para revisión humana simulada, sin aprobar condiciones ni contactar a un asesor real."
    elif reason == "dispute":
        text = "Registro que no reconoces la obligación y una solicitud de revisión humana simulada. Finalizo la negociación de esta sesión."
    else:
        text = "Registro una solicitud de atención humana simulada y finalizo esta sesión. No se ha contactado a un asesor real."
    return {"session": session, "response_text": text}
