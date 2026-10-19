"""Interpreta lenguaje; las autorizaciones y cálculos pertenecen al backend."""

import json

from anthropic import APIError
from langgraph.runtime import Runtime

from agents.collections.interpreter import parse_turn
from agents.collections.runtime import AppContext, resolve_context
from agents.collections.semantic_state import current_understanding, matches_confirmation_context
from agents.collections.state import State


def interpret_turn(state: State, runtime: Runtime[AppContext]) -> dict:
    ctx = resolve_context(runtime)
    session = dict(state["session"])
    understanding = current_understanding(state)
    try:
        if understanding is not None:
            if understanding.status == "model_error":
                raise ValueError("Semantic interpretation unavailable")
            parsed = understanding.turn
        else:
            parsed = parse_turn(
                state["text"],
                today=ctx.today,
                provider=ctx.provider,
                model=ctx.model,
                context=json.dumps(
                    {
                        "stage": session["stage"],
                        "terms": session["draft_terms"],
                        "dialogue": session.get("dialogue", {}),
                        "recent_turns": session.get("history", [])[-4:],
                    },
                    ensure_ascii=False,
                ),
            )
    except (APIError, ValueError, TimeoutError, ConnectionError):
        return {
            "intent_route": "response",
            "response_text": "No pude interpretar la respuesta por un error del servicio. No registré ningún acuerdo; puedes reintentar.",
        }
    if understanding is not None:
        if parsed.intent == "accept_offer" and (
            parsed.acceptance != "unconditional_current_offer"
            or session["stage"] != "awaiting_confirmation"
            or not session.get("pending_offer_id")
            or not matches_confirmation_context(state)
            or parsed.amount_is_capacity
            or parsed.frequency != "unspecified"
            or any(
                value is not None
                for value in (parsed.amount_cop, parsed.payment_date, parsed.installments)
            )
        ):
            parsed = type(parsed).model_validate(
                {
                    **parsed.model_dump(),
                    "intent": "unclear",
                    "acceptance": "conditional" if parsed.acceptance == "conditional" else "none",
                }
            )
        if parsed.acceptance == "conditional" and parsed.intent in {"accept_offer", "unclear"}:
            parsed = parsed.model_copy(update={"intent": "reject_offer"})
        if (
            session.get("dialogue", {}).get("awaiting") == "frequency"
            and parsed.frequency in {"monthly", "one_off"}
            and parsed.intent in {"unclear", "counteroffer", "promise", "negotiate"}
        ):
            amount = parsed.amount_cop or session["draft_terms"].get("amount_cop")
            if amount:
                monthly = parsed.frequency == "monthly"
                parsed = parsed.model_copy(
                    update={
                        "intent": "counteroffer" if monthly else "promise",
                        "amount_cop": amount,
                        "amount_is_capacity": monthly,
                    }
                )
        if parsed.intent in {"options", "continue", "no_capacity", "scope", "history", "greeting"}:
            if parsed.amount_cop is not None and parsed.amount_is_capacity:
                session["dialogue"] = {
                    **session.get("dialogue", {}),
                    "monthly_capacity_cop": parsed.amount_cop,
                }
            session.update(pending_offer_id=None, draft_terms={}, stage="ready")
            return {
                "session": session,
                "interpretation": parsed.model_dump(mode="json"),
                "intent_route": "negotiation",
                "response_text": "",
            }
    if parsed.intent in {"off_topic", "complaint", "clarify_process"}:
        # A conversational "yes" must not accept an earlier financial proposal.
        session["pending_offer_id"] = None
        session["stage"] = "awaiting_terms" if session["draft_terms"] else "ready"
        return {
            "session": session,
            "interpretation": parsed.model_dump(mode="json"),
            "intent_route": "negotiation",
            "response_text": "",
        }
    if (
        understanding is None
        and session.get("dialogue", {}).get("awaiting") == "frequency"
        and state["text"].strip().casefold() in {"al mes", "cada mes", "mensual"}
    ):
        amount = session["draft_terms"].get("amount_cop")
        if amount:
            parsed = parsed.model_copy(
                update={"intent": "counteroffer", "amount_cop": amount, "amount_is_capacity": True}
            )
    if (
        understanding is None
        and session.get("dialogue", {}).get("awaiting") == "frequency"
        and state["text"].strip().casefold()
        in {
            "un único abono",
            "un unico abono",
            "un abono",
            "una sola vez",
            "único",
            "unico",
        }
    ):
        amount = session["draft_terms"].get("amount_cop")
        if amount:
            parsed = parsed.model_copy(
                update={"intent": "promise", "amount_cop": amount, "amount_is_capacity": False}
            )
    # A bare amount answers the preceding capacity question, never accepts an offer.
    if (
        understanding is None
        and parsed.intent == "unclear"
        and parsed.amount_cop
        and session.get("dialogue", {}).get("awaiting") == "capacity"
    ):
        parsed = parsed.model_copy(update={"intent": "counteroffer", "amount_is_capacity": True})
    if parsed.amount_cop and parsed.amount_is_capacity:
        session["dialogue"] = {
            **session.get("dialogue", {}),
            "monthly_capacity_cop": parsed.amount_cop,
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
        "decline": "close",
        "identity": "response",
        "verification_code": "response",
        "unclear": "response",
        "refuse": "negotiation",
        "hardship": "negotiation",
        "reject_offer": "negotiation",
        "counteroffer": "negotiation",
        "request_exception": "handoff",
    }[parsed.intent]
    has_terms = any(
        value is not None for value in (parsed.amount_cop, parsed.payment_date, parsed.installments)
    )
    if route == "response" and has_terms and session["draft_terms"]:
        route = session["draft_terms"].get("route", "promise")
    if session.get("pending_offer_id") and parsed.intent in {
        "refuse",
        "hardship",
        "reject_offer",
        "counteroffer",
        "request_exception",
    }:
        offer = ctx.repository.get_offer(session["pending_offer_id"], state["session_id"])
        rejected = session.get("rejected_offers", [])
        session["rejected_offers"] = [*rejected, offer.model_dump(mode="json")][-3:]
        session.update(pending_offer_id=None, draft_terms={})
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
        if parsed.amount_cop is not None:
            terms["amount_is_capacity"] = parsed.amount_is_capacity
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
        response = (
            "Quiero entenderte mejor. ¿Qué te preocupa de la deuda o de la propuesta?"
            if session["clarifications"] == 1
            else "Podemos revisar tu capacidad de pago, solicitar un asesor o terminar. ¿Qué prefieres?"
        )
    else:
        response = ""
    return {
        "session": session,
        "interpretation": interpretation,
        "intent_route": route,
        "response_text": response,
    }
