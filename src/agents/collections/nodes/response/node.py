"""Persiste acuerdo, sesión y evento juntos antes de entregar la respuesta."""

from langgraph.runtime import Runtime

from agents.collections.runtime import AppContext, resolve_context
from agents.collections.state import State


def compose_response(state: State, runtime: Runtime[AppContext]) -> dict:
    if state.get("replay"):
        return {"reply": state["reply"]}
    ctx = resolve_context(runtime)
    session = dict(state["session"])
    with ctx.repository.connection():
        if state.get("commit_requested"):
            agreement_id = ctx.repository.save_confirmed_offer(
                session_id=state["session_id"],
                offer_id=session["pending_offer_id"],
                event_id=state["event_id"],
                today=ctx.today,
            )
            session.update(stage="agreed", agreement_id=agreement_id)
            text = f"Registré tu compromiso ficticio con referencia {agreement_id}. Esto es una promesa de pago: el saldo continúa pendiente hasta verificar un pago. Gracias."
        else:
            text = state["response_text"]
        if not text:
            raise ValueError("El workflow no produjo una respuesta validada.")
        session["turns"] += 1
        session["history"] = [
            *session["history"],
            {"event_id": state["event_id"], "user": state.get("text", ""), "assistant": text},
        ]
        reply = {
            "session_id": state["session_id"],
            "text": text,
            "wording_status": state.get("wording_status", "deterministic"),
            "stage": session["stage"],
            "offer_id": session.get("pending_offer_id"),
            "agreement_id": session.get("agreement_id"),
            "finished": session["stage"] in {"agreed", "closed", "handoff"},
        }
        ctx.repository.save_turn({**state, "session": session}, reply)
    return {"session": session, "reply": reply}
