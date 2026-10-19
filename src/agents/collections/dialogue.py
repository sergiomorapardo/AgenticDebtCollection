"""Choose the next conversational step using persisted, bounded negotiation memory."""

from agents.collections.policy import build_offer, load_policy
from agents.collections.runtime import resolve_context
from agents.collections.semantic_state import current_understanding


def plan_dialogue(state, runtime):
    session = dict(state["session"])
    intent = state.get("interpretation", {}).get("intent")
    dialogue = dict(session.get("dialogue", {}))
    understanding = current_understanding(state)
    if understanding is None or understanding.status == "model_error":
        return {
            "session": session,
            "intent_route": "response",
            "response_text": "No pude interpretar tu respuesta. No registré cambios; puedes reintentar.",
        }
    if intent in {"options", "continue"}:
        if not session.get("identity_verified"):
            return {
                "session": session,
                "intent_route": "response",
                "response_text": "Antes de consultar opciones de la cuenta necesito verificar tu identidad.",
            }
        ctx = resolve_context(runtime)
        account = ctx.repository.get_account(session["account_id"])
        policy = load_policy()
        choices = []
        for count in range(1, policy.max_installments + 1):
            try:
                option = build_offer(
                    account,
                    policy,
                    today=ctx.today,
                    first_payment_date=ctx.today,
                    installments=count,
                )
            except ValueError:
                continue
            capacity = dialogue.get("monthly_capacity_cop")
            if capacity is not None and any(
                item.amount_cop > capacity for item in option.installments
            ):
                continue
            amounts = ", ".join(f"{item.amount_cop:,} COP" for item in option.installments)
            label = "un pago" if count == 1 else f"{count} cuotas mensuales"
            choices.append(f"{label}: {amounts}")
        session.update(pending_offer_id=None, draft_terms={}, stage="ready")
        dialogue["awaiting"] = "option"
        session["dialogue"] = dialogue
        if not choices:
            session.update(stage="handoff", handoff_reason="no_viable_plan")
            dialogue["awaiting"] = None
            text = "No encontré un plan disponible que se ajuste a tu capacidad. Dejo una solicitud de revisión, sin registrar un acuerdo."
        else:
            text = (
                "Puedes pagar en "
                + "; ".join(choices)
                + f". El primer pago debe ser dentro de {policy.first_payment_within_days} días. "
                "Estos importes son orientativos, no hay una oferta para aceptar: elige una opción y una fecha para revisar "
                "el calendario completo antes de confirmar."
            )
        return {"session": session, "intent_route": "response", "response_text": text}
    if intent in {"scope", "history", "greeting"}:
        return {
            "session": session,
            "intent_route": "response",
            "response_text": "Puedo ayudarte a revisar tu cuenta, las opciones de pago o solicitar atención de un asesor. ¿Qué te gustaría revisar?",
        }
    if intent in {"off_topic", "complaint", "clarify_process"}:
        responses = {
            "off_topic": "Puedo ayudarte con tu cuenta y las opciones de pago, pero no con programación u otros temas. La sesión sigue abierta. ¿Quieres retomar lo que te preocupa de la deuda?",
            "complaint": "Lamento que la atención te haya resultado frustrante. No interpretaré tu queja como una negativa a pagar. ¿Qué parte de la atención te gustaría que aclare o corrija?",
            "clarify_process": "La sesión sigue abierta. Podemos aclarar una duda sobre la deuda, revisar opciones de pago, dejar una solicitud de atención de un asesor o terminar si lo pides. ¿Qué necesitas aclarar?",
        }
        return {"session": session, "intent_route": "response", "response_text": responses[intent]}
    if intent == "no_capacity":
        session.update(
            stage="handoff", pending_offer_id=None, draft_terms={}, handoff_reason="no_capacity"
        )
        dialogue["awaiting"] = None
        session["dialogue"] = dialogue
        return {
            "session": session,
            "intent_route": "response",
            "response_text": "Entiendo que ahora no tienes capacidad de pago. No propondré cuotas que no puedes asumir. Dejo una solicitud de revisión, sin registrar un acuerdo.",
        }
    if intent not in {"refuse", "hardship", "reject_offer", "counteroffer", "negotiate"}:
        return {}
    dialogue["last_intent"] = intent
    if intent == "refuse":
        dialogue["refusals"] = dialogue.get("refusals", 0) + 1
        session.update(pending_offer_id=None, draft_terms={}, stage="ready")
        if dialogue["refusals"] >= 2:
            session["stage"] = "closed"
            session["close_reason"] = "repeated_refusal"
            text = "Entiendo que no deseas pagar. No insistiré; cierro esta conversación sin registrar un acuerdo ni modificar el saldo."
            dialogue["awaiting"] = None
        else:
            text = "Entiendo. ¿Se debe a una dificultad económica o a que no estás de acuerdo con la deuda? Si prefieres terminar, lo respetaré."
            dialogue["awaiting"] = "objection"
    elif intent in {"hardship", "reject_offer"}:
        session.update(pending_offer_id=None, draft_terms={}, stage="awaiting_terms")
        dialogue["objections"] = dialogue.get("objections", 0) + 1
        if dialogue["objections"] >= 3:
            session["stage"] = "handoff"
            session["handoff_reason"] = "no_viable_plan"
            text = "No hemos encontrado un plan que se ajuste a tu situación. Dejo una solicitud de revisión, sin registrar un acuerdo. No puedo transferirte a un asesor desde aquí."
            dialogue["awaiting"] = None
        else:
            prefix = (
                "Lamento que estés pasando por esa dificultad."
                if intent == "hardship"
                else "Dejamos esa propuesta de lado."
            )
            text = (
                prefix
                + " ¿Qué monto mensual podrías destinar sin afectar tus gastos esenciales? Si ahora no puedes destinar nada, dímelo."
            )
            dialogue["awaiting"] = "capacity"
    else:
        terms = session["draft_terms"]
        if (
            terms.get("amount_cop")
            and not terms.get("amount_is_capacity")
            and not terms.get("installments")
        ):
            # A one-off amount cannot silently become a monthly commitment.
            text = "¿Ese monto es lo que podrías pagar cada mes o sería un único abono?"
            dialogue["awaiting"] = "frequency"
        elif not terms.get("amount_cop") and not terms.get("installments"):
            text = "Para buscar una alternativa, ¿cuánto podrías pagar al mes sin afectar tus gastos esenciales?"
            dialogue["awaiting"] = "capacity"
        else:
            dialogue["awaiting"] = "date" if not terms.get("payment_date") else "confirmation"
            session["dialogue"] = dialogue
            return {"session": session}
    session["dialogue"] = dialogue
    return {"session": session, "intent_route": "response", "response_text": text}
