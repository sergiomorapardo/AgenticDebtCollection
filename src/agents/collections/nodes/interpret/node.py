"""Interpreta lenguaje; las autorizaciones y cálculos pertenecen al backend."""

from langgraph.runtime import Runtime

from agents.collections.runtime import AppContext, resolve_context
from agents.collections.semantic import SPANISH_MONTHS
from agents.collections.semantic_state import current_understanding, matches_confirmation_context
from agents.collections.state import State


def _clarify_payment_date(session, parsed, today):
    """Retain chosen terms while removing any authority to confirm an ambiguous date."""
    from datetime import timedelta

    terms = dict(session.get("draft_terms", {}))
    terms.pop("payment_date", None)
    for key in ("amount_cop", "installments"):
        if (value := getattr(parsed, key)) is not None:
            terms[key] = value
    if parsed.amount_cop is not None:
        terms["amount_is_capacity"] = parsed.amount_is_capacity
    terms.setdefault("route", "promise" if parsed.intent == "promise" else "negotiation")
    reference = parsed.date_reference
    reason = parsed.date_clarification
    if reason == "month":
        question = f"Cuando dices el {reference.day}, ¿de qué mes y año hablamos?"
    elif reason == "year":
        question = f"¿De qué año es el {reference.day} de {SPANISH_MONTHS[reference.month - 1]}?"
    elif reason == "weekday":
        question = f"¿Te refieres a hoy, {today.isoformat()}, o al mismo día de la próxima semana, {(today + timedelta(days=7)).isoformat()}?"
    elif reason == "multiple":
        question = "La fecha tiene más de una interpretación posible. ¿Qué día, mes y año eliges para el primer pago?"
    else:
        question = "Esa fecha no existe en el calendario. ¿Qué día, mes y año quieres indicar?"
    session.update(stage="awaiting_terms", pending_offer_id=None, draft_terms=terms)
    session["dialogue"] = {
        **session.get("dialogue", {}),
        "awaiting": "date",
        "pending_date": reference.model_dump(mode="json"),
    }
    return {
        "session": session,
        "interpretation": {**parsed.model_dump(mode="json"), "intent": "unclear"},
        "intent_route": "response",
        "response_text": question
        + (f" Hoy es {today.isoformat()}." if reason != "weekday" else ""),
    }


def _replay_unheard_offer(state, session, ctx):
    """Keep an unheard financial offer out of the confirmation route."""
    from agents.collections.policy import load_policy, validate_offer

    try:
        offer = ctx.repository.get_offer(session["pending_offer_id"], state["session_id"])
        validate_offer(
            offer, ctx.repository.get_account(session["account_id"]), load_policy(), today=ctx.today
        )
    except ValueError:
        session.update(pending_offer_id=None, stage="ready", draft_terms={})
        text = "La oferta dejó de ser válida o el saldo cambió. Debemos calcular y confirmar una nueva; no guardé un acuerdo."
    else:
        schedule = "; ".join(
            f"{item.amount_cop:,} COP el {item.due_date.isoformat()}" for item in offer.installments
        )
        total = sum(item.amount_cop for item in offer.installments)
        text = f"La oferta se interrumpió. Escúchala completa antes de confirmar. Propuesta: {schedule}. Total: {total:,} COP, sin descuentos ni cargos adicionales. ¿Confirmas este importe y estas fechas? Responde confirmo o propone cambios."
    return {"session": session, "intent_route": "response", "response_text": text}


def interpret_turn(state: State, runtime: Runtime[AppContext]) -> dict:
    ctx = resolve_context(runtime)
    session = dict(state["session"])
    understanding = current_understanding(state)
    if understanding is None or understanding.status == "model_error":
        return {
            "intent_route": "response",
            "response_text": "No pude interpretar la respuesta por un error del servicio. No registré ningún acuerdo; puedes reintentar.",
        }
    parsed = understanding.turn
    if parsed.date_clarification and parsed.date_reference is not None:
        return _clarify_payment_date(session, parsed, ctx.today)
    if parsed.payment_date is not None:
        session["dialogue"] = dict(session.get("dialogue", {}))
        session["dialogue"].pop("pending_date", None)
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
    if parsed.intent == "accept_offer" and not ctx.offer_audio_completed:
        return {
            **_replay_unheard_offer(state, session, ctx),
            "interpretation": parsed.model_dump(mode="json"),
        }
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
    financial_intents = {"promise", "negotiate", "counteroffer", "unclear"}
    if (
        parsed.intent in financial_intents
        and parsed.amount_cop is not None
        and parsed.amount_is_capacity
        and parsed.frequency != "one_off"
    ):
        # A recurring capacity is not a promise to settle the full balance.
        parsed = parsed.model_copy(update={"intent": "counteroffer"})
    previous_route = session.get("draft_terms", {}).get("route")
    if (
        parsed.intent in financial_intents
        and parsed.payment_date is not None
        and parsed.amount_cop is None
        and parsed.installments is None
        and parsed.frequency == "unspecified"
        and previous_route in {"promise", "negotiation"}
    ):
        # A date answer completes the backend's selected terms, not a new payment type.
        parsed = parsed.model_copy(
            update={"intent": "promise" if previous_route == "promise" else "negotiate"}
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
