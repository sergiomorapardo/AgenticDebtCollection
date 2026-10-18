"""Preparación de ofertas compartida por pago completo y negociación."""

from datetime import date

from agents.collections.policy import build_offer, load_policy


def prepare_offer(state, ctx, *, negotiation: bool) -> dict:
    session = dict(state["session"])
    terms = session["draft_terms"]
    account = ctx.repository.get_account(session["account_id"])
    policy = load_policy()
    count = terms.get("installments") or (None if negotiation else 1)
    amount = terms.get("amount_cop")
    if negotiation and count is None and amount and account.balance_cop % amount == 0:
        count = account.balance_cop // amount
    if not negotiation and amount is not None and amount != account.balance_cop:
        return {
            "response_text": "Ese importe no cubre el saldo completo. Puedes solicitar un plan de dos o tres cuotas o revisión humana; todavía no hay oferta."
        }
    if count is None:
        return {
            "response_text": "La política ficticia permite dos o tres cuotas mensuales, sin descuento ni cargos adicionales. ¿Cuál opción prefieres y en qué fecha harías el primer pago?"
        }
    if not 1 <= count <= policy.max_installments:
        session["stage"] = "handoff"
        return {
            "session": session,
            "response_text": "La propuesta supera la política ficticia. Registro una solicitud de revisión humana simulada; no hay acuerdo automático.",
        }
    if amount is not None and negotiation and amount * count != account.balance_cop:
        return {
            "response_text": "Los importes propuestos no suman el saldo. Indica solo el número de cuotas para calcular un calendario exacto, o solicita un asesor."
        }
    if not terms.get("payment_date"):
        return {
            "response_text": "¿Qué fecha propones para el primer pago? Usa AAAA-MM-DD, hoy o mañana; debe estar dentro de los próximos siete días."
        }
    try:
        offer = build_offer(
            account,
            policy,
            today=ctx.today,
            first_payment_date=date.fromisoformat(terms["payment_date"]),
            installments=count,
        )
    except ValueError:
        session["draft_terms"].pop("payment_date", None)
        return {
            "session": session,
            "response_text": "La fecha o el plan no cumplen la política ficticia. El primer pago debe ser entre hoy y siete días; indica una fecha válida.",
        }
    session["negotiations"] += 1
    if session["negotiations"] > 3:
        session["stage"] = "handoff"
        return {
            "session": session,
            "response_text": "Para revisar otras condiciones, registro una solicitud de atención humana simulada.",
        }
    # El hash de términos es determinista; la referencia presentada pertenece a esta sesión.
    session["pending_offer_id"] = offer.offer_id
    session["stage"] = "awaiting_confirmation"
    schedule = "; ".join(
        f"{item.amount_cop:,} COP el {item.due_date.isoformat()}" for item in offer.installments
    )
    response = f"Propuesta ficticia: {schedule}. Total: {account.balance_cop:,} COP, sin descuentos ni cargos adicionales. ¿Confirmas este importe y estas fechas? Responde confirmo o propone cambios."
    return {
        "session": session,
        "draft_offer": offer.model_dump(mode="json"),
        "response_text": response,
    }
