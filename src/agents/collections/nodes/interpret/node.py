"""Interpreta lenguaje; las autorizaciones y cálculos pertenecen al backend."""

from anthropic import APIError
from langgraph.runtime import Runtime

from agents.collections.interpreter import parse_turn
from agents.collections.runtime import AppContext, resolve_context
from agents.collections.state import State


def interpret_turn(state: State, runtime: Runtime[AppContext]) -> dict:
    ctx = resolve_context(runtime)
    session = dict(state["session"])
    try:
        parsed = parse_turn(
            state["text"],
            today=ctx.today,
            provider=ctx.provider,
            model=ctx.model,
            context=f"Etapa: {session['stage']}; términos pendientes: {session['draft_terms']}",
        )
    except (APIError, ValueError, TimeoutError, ConnectionError):
        return {
            "intent_route": "response",
            "response_text": "No pude interpretar la respuesta por un error del servicio. No registré ningún acuerdo; puedes reintentar.",
        }
    interpretation = parsed.model_dump(mode="json")
    route = {
        "promise": "promise",
        "negotiate": "negotiation",
        "accept_offer": "confirmation",
        "reported_payment": "handoff",
        "dispute": "handoff",
        "request_human": "handoff",
        "end": "close",
        "unclear": "response",
    }[parsed.intent]
    has_terms = any(
        value is not None for value in (parsed.amount_cop, parsed.payment_date, parsed.installments)
    )
    if route == "response" and has_terms and session["draft_terms"]:
        route = session["draft_terms"].get("route", "promise")
    if route in {"promise", "negotiation"}:
        session["pending_offer_id"] = None
        previous = session.get("draft_terms", {})
        terms = dict(previous) if previous.get("route") == route else {}
        if parsed.installments is not None and parsed.amount_cop is None:
            terms.pop("amount_cop", None)
        if parsed.amount_cop is not None and parsed.installments is None and route == "negotiation":
            terms.pop("installments", None)
        terms.update(
            {
                key: value
                for key, value in interpretation.items()
                if key in {"amount_cop", "payment_date", "installments"} and value is not None
            }
        )
        terms["route"] = route
        session["draft_terms"] = terms
        session["stage"] = "awaiting_terms"
        session["clarifications"] = 0
    if route == "response":
        # Una aclaración o rechazo cambia el contexto de la siguiente respuesta.
        # Se requiere volver a presentar términos antes de aceptar un sí posterior.
        if session.get("pending_offer_id"):
            session.update(pending_offer_id=None, stage="ready", draft_terms={})
        session["clarifications"] += 1
        if session["clarifications"] >= 3:
            route = "handoff"
        response = "Necesito una respuesta más precisa: indica pago completo o el número de cuotas y una fecha AAAA-MM-DD. No registré ningún acuerdo."
    else:
        response = ""
    return {
        "session": session,
        "interpretation": interpretation,
        "intent_route": route,
        "response_text": response,
    }
