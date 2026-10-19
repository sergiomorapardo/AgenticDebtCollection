"""Interpret new human turns once using backend-owned stage and account context."""

from agents.collections import semantic
from agents.collections.chat_memory import ChatMemory
from agents.collections.conversation import recoverable_misclosure
from agents.collections.memory import event_hash
from agents.collections.runtime import resolve_context
from agents.collections.semantic_state import bind_understanding


def _message_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    return ""


def _latest_assistant(history):
    for item in reversed(history):
        if isinstance(item, dict):
            if "assistant" in item:
                return _message_text(item["assistant"])
            if item.get("role") in {"ai", "assistant"}:
                return _message_text(item.get("text", item.get("content", "")))
        elif getattr(item, "type", None) == "ai":
            return _message_text(item.content)
    return ""


def _session_context(repo, session, session_id, *, verified):
    context = {"identity_verified": verified}
    if not verified or session is None:
        return context
    context.update(
        recoverable_misclosure=recoverable_misclosure(session),
        draft_terms=session.get("draft_terms", {}),
        dialogue=session.get("dialogue", {}),
    )
    if session.get("pending_offer_id") and session["stage"] == "awaiting_confirmation":
        try:
            offer = repo.get_offer(session["pending_offer_id"], session_id)
        except ValueError:
            # An absent/unpresented proposal supplies no context or permission to accept.
            pass
        else:
            context["pending_offer"] = offer.model_dump(mode="json")
    return context


def understand_message(state, runtime):
    """Replace stale meaning on every entry; replay and empty CLI bootstrap never call an LLM."""
    ctx = resolve_context(runtime)
    repo = ctx.repository
    cleared = {"semantic_result": None}
    text = state.get("text", "")
    if not isinstance(text, str) or len(text) > 4000:
        raise ValueError("Send at most 4000 text characters per turn.")
    if not state.get("event_id") or not state.get("session_id"):
        raise ValueError("The backend requires session and event identifiers.")
    account_id = state.get("account_id", "demo-001")
    session_id = state["session_id"]
    is_studio = bool(state.get("chat_thread"))
    if is_studio:
        if state.get("chat_reply") is not None:
            return cleared
        binding = ChatMemory(repo).binding(state["chat_thread"])
        if binding["account_id"] != account_id:
            raise ValueError("The backend account binding changed.")
        verified = binding["verified"]
        session = (
            repo.get_session(binding["session_id"]) if verified and binding["session_id"] else None
        )
        if session is not None and session["account_id"] != account_id:
            raise ValueError("Una sesión no puede cambiar de cuenta.")
        if binding["closed"]:
            stage = "closed"
        elif not binding["account_id"]:
            stage = "awaiting_customer"
        elif not verified:
            stage = "awaiting_identity"
        else:
            stage = session["stage"] if session is not None else "ready"
        history = list(state.get("messages", []))[:-1]
        if not history:
            history = (
                session.get("history", []) if session else binding.get("onboarding_history", [])
            )
        context = _session_context(repo, session, binding["session_id"], verified=verified)
    else:
        digest = event_hash(account_id, text.strip())
        if repo.get_event(session_id, state["event_id"], digest) is not None:
            return cleared
        session = repo.get_session(session_id)
        if session is not None and session["account_id"] != account_id:
            raise ValueError("Una sesión no puede cambiar de cuenta.")
        if not text.strip():
            return cleared
        # A public caller cannot use a made-up account to invoke the model.
        repo.get_account(account_id)
        stage = session["stage"] if session is not None else "awaiting_identity"
        verified = bool(session and session.get("identity_verified"))
        history = session.get("history", []) if session else []
        context = _session_context(repo, session, session_id, verified=verified)
    context["latest_assistant_prompt"] = _latest_assistant(history)
    result = semantic.understand_turn(
        text, ctx=ctx, today=ctx.today, stage=stage, history=history, context=context
    )
    if not is_studio and session is None and result.status == "model_error":
        raise ValueError(
            "Could not interpret this message. Retry before starting a collection session."
        )
    return {"semantic_result": bind_understanding(state, result)}
