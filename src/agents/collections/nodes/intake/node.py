"""Recupera estado confiable y controla el comienzo de la simulación."""

from langgraph.runtime import Runtime

from agents.collections.contact import check_contact
from agents.collections.memory import event_hash
from agents.collections.policy import load_policy
from agents.collections.runtime import AppContext, resolve_context
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
    session = repo.get_session(session_id)
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
        allowed, reason = check_contact(
            now=ctx.now,
            authorized=repo.is_contact_authorized(account_id),
            prior_contacts=repo.contacts_for(account_id),
        )
        if not allowed:
            session["stage"] = "closed"
            return {
                **base,
                "session": session,
                "response_text": f"La simulación no puede iniciar por la regla de contacto: {reason}.",
            }
        session["contact_started_at"] = ctx.now.isoformat()
        return {
            **base,
            "session": session,
            "response_text": "Hola, soy el asistente automatizado de la fintech ficticia. Esta es una simulación. Para continuar, introduce verificar y el código ficticio del escenario; no uses datos personales reales.",
        }
    if session["account_id"] != account_id:
        raise ValueError("Una sesión no puede cambiar de cuenta.")
    base["session"] = session
    if session["stage"] in {"closed", "handoff", "agreed"}:
        return {
            **base,
            "response_text": "Esta sesión ya finalizó. Su resultado está registrado; no se realizará otra gestión en esta sesión.",
        }
    if text.casefold() in {
        "terminar",
        "adiós",
        "adios",
        "salir",
        "no me llames",
        "no me llamen",
        "no quiero continuar",
        "cuelga",
    }:
        return {**base, "intake_route": "close"}
    if session["turns"] >= 20:
        session["stage"] = "handoff"
        return {
            **base,
            "response_text": "Alcanzamos el límite de la simulación. Dejo una solicitud de revisión humana simulada.",
        }
    if not session["identity_verified"]:
        if verify_demo_code(account_id, text):
            session["identity_verified"] = True
            session["stage"] = "ready"
            account = repo.get_account(account_id)
            return {
                **base,
                "response_text": f"Identidad ficticia verificada. El saldo registrado es {account.balance_cop:,} COP. ¿Puedes pagar el total en los próximos siete días o prefieres explorar un plan de dos o tres cuotas?",
            }
        session["identity_attempts"] += 1
        if session["identity_attempts"] >= 3:
            session["stage"] = "closed"
            return {
                **base,
                "response_text": "No pude verificar la identidad ficticia. Finalizo sin divulgar información de la obligación.",
            }
        return {
            **base,
            "response_text": "La verificación ficticia no coincide. Usa verificar y el código del escenario.",
        }
    return {**base, "intake_route": "interpret"}
