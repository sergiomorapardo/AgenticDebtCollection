"""Verifica aceptación de una oferta presentada y aún válida."""

from langgraph.runtime import Runtime

from agents.collections.policy import load_policy, validate_offer
from agents.collections.runtime import AppContext, resolve_context
from agents.collections.state import State


def validate_confirmation(state: State, runtime: Runtime[AppContext]) -> dict:
    ctx = resolve_context(runtime)
    session = dict(state["session"])
    if session["stage"] != "awaiting_confirmation" or not session.get("pending_offer_id"):
        return {
            "confirmation_route": "response",
            "response_text": "Primero debemos definir y presentar un importe y unas fechas concretos. No hay una oferta para confirmar.",
        }
    try:
        offer = ctx.repository.get_offer(session["pending_offer_id"], state["session_id"])
        validate_offer(
            offer, ctx.repository.get_account(session["account_id"]), load_policy(), today=ctx.today
        )
    except ValueError:
        session.update(pending_offer_id=None, stage="ready", draft_terms={})
        return {
            "session": session,
            "confirmation_route": "response",
            "response_text": "La oferta dejó de ser válida o el saldo cambió. Debemos calcular y confirmar una nueva; no guardé un acuerdo.",
        }
    if not ctx.offer_audio_completed:
        schedule = "; ".join(
            f"{item.amount_cop:,} COP el {item.due_date.isoformat()}" for item in offer.installments
        )
        total = sum(item.amount_cop for item in offer.installments)
        proposal = f"Propuesta: {schedule}. Total: {total:,} COP, sin descuentos ni cargos adicionales. ¿Confirmas este importe y estas fechas? Responde confirmo o propone cambios."
        return {
            "confirmation_route": "response",
            "response_text": "La oferta se interrumpió. Escúchala completa antes de confirmar. "
            + proposal,
        }
    return {"confirmation_route": "commitment", "confirmed_offer": offer.model_dump(mode="json")}
