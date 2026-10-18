"""Choose the next conversational step using persisted, bounded negotiation memory."""


def plan_dialogue(state, runtime):
    session = dict(state["session"])
    intent = state.get("interpretation", {}).get("intent")
    dialogue = dict(session.get("dialogue", {}))
    if dialogue.get("awaiting") == "capacity" and state["text"].strip().lower() in {
        "nada",
        "cero",
        "0",
        "no puedo destinar nada",
    }:
        session.update(
            stage="handoff", pending_offer_id=None, draft_terms={}, handoff_reason="no_capacity"
        )
        dialogue["awaiting"] = None
        session["dialogue"] = dialogue
        return {
            "session": session,
            "intent_route": "response",
            "response_text": "Entiendo que ahora no tienes capacidad de pago. No propondré cuotas que no puedes asumir. Dejo una solicitud de revisión humana simulada, sin registrar un acuerdo.",
        }
    if intent not in {"refuse", "hardship", "reject_offer", "counteroffer", "negotiate"}:
        return {}
    dialogue["last_intent"] = intent
    if intent == "refuse":
        dialogue["refusals"] = dialogue.get("refusals", 0) + 1
        session.update(pending_offer_id=None, draft_terms={}, stage="ready")
        if dialogue["refusals"] >= 2:
            session["stage"] = "closed"
            text = "Entiendo que no deseas pagar. No insistiré; finalizo esta simulación sin registrar un acuerdo ni modificar el saldo."
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
            text = "No hemos encontrado un plan que se ajuste a tu situación. Dejo una solicitud de revisión humana simulada, sin acuerdo ni contacto con un asesor real."
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
