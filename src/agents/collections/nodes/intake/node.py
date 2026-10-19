"""Recupera estado confiable y controla el comienzo de la simulación."""

from langgraph.runtime import Runtime

from agents.collections.contact import check_contact
from agents.collections.conversation import (
    recoverable_misclosure,
    terminal_response,
)
from agents.collections.memory import event_hash
from agents.collections.policy import load_policy
from agents.collections.runtime import AppContext, resolve_context
from agents.collections.semantic_state import current_understanding
from agents.collections.state import State

from .tools import verify_demo_code


def prepare_turn(state: State, runtime: Runtime[AppContext]) -> dict:
    ctx = resolve_context(runtime)
    repo = ctx.repository
    session_id, event_id = state.get("session_id", ""), state.get("event_id", "")
    if not session_id or not event_id:
        raise ValueError("Se requieren session_id y event_id únicos por turno.")
    text = state.get("text", "").strip()
    if len(text) > 4000:
        raise ValueError("Máximo 4000 caracteres por turno.")
    account_id = state.get("account_id", "demo-001")
    digest = event_hash(account_id, text)
    replay = repo.get_event(session_id, event_id, digest)
    base = {
        "replay": False,
        "commit_requested": False,
        "input_hash": digest,
        "draft_offer": None,
        "confirmed_offer": None,
        "interpretation": {},
        "response_text": "",
        "intake_route": "response",
    }
    if replay is not None:
        return {**base, "replay": True, "reply": replay}
    understanding = current_understanding(state)
    session = repo.get_session(session_id)
    valid_meaning = understanding is not None and understanding.status != "model_error"
    if session is None and not valid_meaning and not (understanding is None and not text):
        raise ValueError("A new message requires a valid event-bound semantic interpretation.")
    if session is None:
        repo.get_account(account_id)
        session = {
            "account_id": account_id,
            "identity_verified": False,
            "stage": "awaiting_identity",
            "pending_offer_id": None,
            "agreement_id": None,
            "turns": 0,
            "identity_attempts": 0,
            "clarifications": 0,
            "negotiations": 0,
            "draft_terms": {},
            "history": [],
            "revision": 0,
            "policy_version": load_policy().policy_version,
        }
        allowed, _reason = check_contact(
            now=ctx.now,
            authorized=repo.is_contact_authorized(account_id),
            prior_contacts=repo.contacts_for(account_id),
        )
        if not allowed:
            session["stage"] = "closed"
            return {
                **base,
                "session": session,
                "response_text": "No puedo iniciar esta gestión en este momento. El contacto no está autorizado para este horario o frecuencia.",
            }
        if understanding is not None and understanding.turn.intent in {"end", "decline"}:
            return {**base, "session": session, "intake_route": "close"}
        session["contact_started_at"] = ctx.now.isoformat()
        return {
            **base,
            "session": session,
            "response_text": "Hola, soy el asistente virtual de atención de cartera. Para consultar tu cuenta, necesito verificar tu identidad.",
        }
    if session["account_id"] != account_id:
        raise ValueError("Una sesión no puede cambiar de cuenta.")
    base["session"] = session
    if not valid_meaning:
        return {
            **base,
            "response_text": "No pude interpretar tu respuesta. No registré cambios; puedes reintentar.",
        }
    if recoverable_misclosure(session):
        from agents.collections.continuation import interpret_continuation

        intent, status = interpret_continuation(state, session, ctx)
        if status == "model_error":
            return {
                **base,
                "response_text": "No pude entender tu respuesta en este momento. ¿Quieres que sigamos revisando las opciones de pago?",
            }
        if intent in {"stop", "decline"}:
            session["close_reason"] = "customer_request"
            return {
                **base,
                "response_text": "Entendido. Mantengo cerrada esta conversación; no retomaré la negociación.",
            }
        if intent == "resume":
            session.update(
                stage="ready",
                pending_offer_id=None,
                draft_terms={},
                clarifications=0,
                close_reason=None,
                recovered_from="legacy_off_topic_closure",
            )
            session["dialogue"] = {**session.get("dialogue", {}), "refusals": 0, "awaiting": None}
            return {**base, "intake_route": "interpret"}
    if session["stage"] in {"closed", "handoff", "agreed"}:
        return {
            **base,
            "response_text": terminal_response(session, text, intent=understanding.turn.intent),
        }
    if understanding.turn.intent in {"end", "decline"}:
        return {**base, "intake_route": "close"}
    if session["turns"] >= 20:
        session["stage"] = "handoff"
        return {
            **base,
            "response_text": "No hemos podido resolver la gestión en esta conversación. Dejo registrada una solicitud de revisión.",
        }
    if not session["identity_verified"]:
        candidate = understanding.verification_candidate
        if understanding.turn.intent != "verification_code" or candidate is None:
            return {
                **base,
                "response_text": "Antes de consultar la cuenta necesito verificar tu identidad. Comparte el código de verificación o dime si prefieres terminar.",
            }
        verification_text = f"verificar {candidate}"
        if verify_demo_code(account_id, verification_text):
            session["identity_verified"] = True
            session["stage"] = "ready"
            account = repo.get_account(account_id)
            return {
                **base,
                "response_text": f"Gracias, verifiqué tu identidad. El saldo registrado es {account.balance_cop:,} COP. ¿Puedes pagar el total en los próximos siete días o prefieres explorar un plan de dos o tres cuotas?",
            }
        session["identity_attempts"] += 1
        if session["identity_attempts"] >= 3:
            session["stage"] = "closed"
            return {
                **base,
                "response_text": "No pude verificar tu identidad. Finalizo sin divulgar información de la obligación.",
            }
        return {
            **base,
            "response_text": "El código de verificación no coincide. Revisa el código e inténtalo de nuevo.",
        }
    return {**base, "intake_route": "interpret"}
