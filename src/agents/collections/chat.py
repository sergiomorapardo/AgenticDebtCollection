"""Human-message entry and visible identity/memory nodes for Studio."""

import hashlib
import json
import re
from typing import Annotated

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from typing_extensions import TypedDict

from agents.collections.chat_memory import ChatMemory, customer_history_reply, summarize_history
from agents.collections.config import data_directory
from agents.collections.conversation import normalize_text, requests_end
from agents.collections.nodes.intake.tools import verify_demo_code
from agents.collections.onboarding import onboarding_reply
from agents.collections.runtime import resolve_context
from agents.collections.state import State


class ChatInput(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


class ChatState(State, ChatInput, total=False):
    chat_thread: str
    chat_event: str
    chat_digest: str
    chat_route: str
    chat_reply: dict | None
    history_summary: str


def reply(text, stage="awaiting_customer", finished=False):
    return {"text": text, "stage": stage, "finished": finished}


def human_input(state: ChatState, config: RunnableConfig, runtime: Runtime):
    messages = state.get("messages", [])
    human_messages = [message for message in messages if isinstance(message, HumanMessage)]
    if not human_messages:
        raise ValueError("Send a human message to continue the conversation.")
    message = human_messages[-1]
    content = message.content
    if isinstance(content, list):
        if not content or any(
            not isinstance(block, dict)
            or block.get("type") != "text"
            or not isinstance(block.get("text"), str)
            for block in content
        ):
            raise ValueError("This simulation accepts text only; attachments are not supported.")
        content = "\n".join(block["text"] for block in content)
    if not isinstance(content, str) or not content.strip() or len(content) > 4000:
        raise ValueError("Send a text message between 1 and 4000 characters.")
    thread = config.get("configurable", {}).get("thread_id")
    if not isinstance(thread, str) or not thread or not message.id:
        raise ValueError("Studio must provide the thread and message identifiers.")
    memory = ChatMemory(resolve_context(runtime).repository)
    digest = hashlib.sha256(content.encode()).hexdigest()
    event = hashlib.sha256(json.dumps([thread, message.id]).encode()).hexdigest()
    cached = memory.replay(thread, event, digest)
    if not isinstance(messages[-1], HumanMessage) and not (
        cached
        and isinstance(messages[-1], AIMessage)
        and messages[-1].id == (cached.get("message_id") or "reply:" + event)
    ):
        raise ValueError("Send a human message to continue the conversation.")
    binding = memory.binding(thread)
    base = {
        "chat_thread": thread,
        "chat_event": event,
        "chat_digest": digest,
        "event_id": event,
        "session_id": binding["session_id"] or "chat:" + thread,
        "account_id": binding["account_id"],
        "text": content.strip(),
        "chat_reply": cached,
        "history_summary": "",
    }
    if cached:
        return {**base, "chat_route": "assistant_message"}
    if binding["closed"]:
        return {
            **base,
            "chat_route": "assistant_message",
            "chat_reply": reply(
                "Esta conversación ya terminó. No consultaré tu cuenta ni registraré nuevos compromisos aquí.",
                "closed",
                True,
            ),
        }
    if not binding["verified"] and requests_end(base["text"]):
        binding["closed"] = True
        response = memory.save_transition(
            base,
            binding,
            reply(
                "Entendido, finalizo la conversación sin consultar tu historial.", "closed", True
            ),
        )
        return {**base, "chat_route": "assistant_message", "chat_reply": response}
    if not binding["account_id"]:
        route = "identify_customer"
    elif not binding["verified"]:
        route = "verify_identity"
    elif not binding["session_id"] or re.search(
        r"\b(historial|resumen|acordamos|acordado)\b", base["text"], re.IGNORECASE
    ):
        route = "recover_history"
    else:
        route = "intake"
    return {**base, "chat_route": route}


def identify_customer(state: ChatState, runtime: Runtime):
    memory = ChatMemory(resolve_context(runtime).repository)
    customers = json.loads((data_directory() / "fixtures/customers.json").read_text())
    normalized = normalize_text(state["text"])
    # Intercept apparent identity/code submissions, not incidental amounts or dates.
    numeric_submission = re.fullmatch(r"[0-9]+(?:\s*(?:[,;]|o|y)\s*[0-9]+)*", normalized)
    identity_submission = re.search(
        r"\b(cedula|identificacion|documento|codigo|verificar)\b|"
        r"\bmi numero\b|\bya te (?:lo )?(?:pase|di|envie|comparti)\b|"
        r"^(?:es|era) [0-9]+$",
        normalized,
    )
    candidates = (
        re.findall(r"(?<!\w)[0-9]+(?!\w)", state["text"])
        if numeric_submission or identity_submission
        else []
    )
    account = customers.get(candidates[0]) if len(candidates) == 1 else None
    if account is None:
        if candidates:
            if len(candidates) > 1:
                text = (
                    "Veo varios números. ¿Cuál es tu número de identificación? Compárteme solo uno."
                )
            elif len(candidates[0]) == 4:
                text = (
                    "Aún no he encontrado tu cuenta. Si ese número es el código de verificación, "
                    "primero necesito tu número de identificación."
                )
            else:
                text = (
                    "No pude encontrar una cuenta con esa identificación. "
                    "Revisa el número o dime si prefieres terminar."
                )
            return {"chat_reply": reply(text)}
        return respond_onboarding(state, runtime)
    with memory.repository.connection():
        binding = memory.binding(state["chat_thread"])
        binding["account_id"] = account
        response = memory.save_transition(
            state,
            binding,
            reply(
                "Antes de consultar tu cuenta, ¿me compartes el código de verificación?",
                "awaiting_identity",
            ),
        )
    return {"account_id": account, "chat_reply": response}


def respond_onboarding(state: ChatState, runtime: Runtime):
    ctx = resolve_context(runtime)
    memory = ChatMemory(ctx.repository)
    binding, response = onboarding_reply(state["text"], memory.binding(state["chat_thread"]), ctx)
    response = memory.save_transition(state, binding, response)
    return {"chat_route": "assistant_message", "chat_reply": response}


def verify_identity(state: ChatState, runtime: Runtime):
    # Only an actual verification submission consumes a verification attempt.
    if not re.fullmatch(r"verificar(?:\s+\d+)?|\d{4}", state["text"], re.IGNORECASE | re.DOTALL):
        return respond_onboarding(state, runtime)
    memory = ChatMemory(resolve_context(runtime).repository)
    with memory.repository.connection():
        binding = memory.binding(state["chat_thread"])
        code_input = state["text"]
        if re.fullmatch(r"\d{4}", code_input):
            code_input = "verificar " + code_input
        if verify_demo_code(binding["account_id"], code_input):
            binding["verified"] = True
            memory.save_binding(state["chat_thread"], binding)
            return {"chat_route": "recover_history"}
        binding["attempts"] += 1
        binding["closed"] = binding["attempts"] >= 3
        closed = binding["closed"]
        response = memory.save_transition(
            state,
            binding,
            reply(
                "El código de verificación no coincide."
                + (
                    " Finalizo sin consultar tu historial."
                    if closed
                    else " Revisa el código e inténtalo de nuevo."
                ),
                "closed" if closed else "awaiting_identity",
                closed,
            ),
        )
    return {"chat_route": "assistant_message", "chat_reply": response}


def recover_history(state: ChatState, runtime: Runtime):
    ctx = resolve_context(runtime)
    memory = ChatMemory(ctx.repository)
    binding = memory.binding(state["chat_thread"])
    if not binding["verified"] or binding["closed"]:
        raise ValueError("Verify identity before accessing customer memory.")
    history = memory.customer_history(binding["account_id"])
    summary = summarize_history(history)
    if history["sessions"]:
        # Resume the same business conversation, with fresh verification for each Studio thread.
        session = history["sessions"][0]
        binding["session_id"] = session["session_id"]
        text = customer_history_reply(history, ctx.repository)
        response = memory.save_transition(
            state,
            binding,
            reply(text, session["stage"], session["stage"] in {"agreed", "closed", "handoff"}),
        )
        return {
            "session_id": binding["session_id"],
            "history_summary": summary,
            "chat_route": "assistant_message",
            "chat_reply": response,
        }
    binding["session_id"] = "chat:" + state["chat_thread"]
    memory.save_binding(state["chat_thread"], binding)
    return {"session_id": binding["session_id"], "history_summary": summary, "chat_route": "intake"}


def studio_intake(state: ChatState, runtime: Runtime):
    """Authorize using backend identity, then use the same collection nodes as the CLI."""
    from agents.collections.nodes.intake.node import prepare_turn

    ctx = resolve_context(runtime)
    binding = ChatMemory(ctx.repository).binding(state["chat_thread"])
    if (
        not binding["verified"]
        or binding["account_id"] != state["account_id"]
        or binding["session_id"] != state["session_id"]
    ):
        raise ValueError("The backend has not verified this conversation.")
    result = prepare_turn(state, runtime)
    session = result.get("session")
    # Only the backend-verified Studio path can start a new session already identified.
    if (
        session
        and session["stage"] == "awaiting_identity"
        and not ctx.repository.get_session(state["session_id"])
    ):
        session.update(identity_verified=True, stage="ready")
        account = ctx.repository.get_account(binding["account_id"])
        result["response_text"] = (
            f"Gracias, verifiqué tu identidad. El saldo registrado es {account.balance_cop:,} COP. ¿Prefieres pagar el total o explorar un plan de dos o tres cuotas?"
        )
    return result


def assistant_message(state: ChatState, runtime: Runtime):
    memory = ChatMemory(resolve_context(runtime).repository)
    response = state.get("chat_reply") or state["reply"]
    response = memory.save_reply(
        state["chat_thread"], state["chat_event"], state["chat_digest"], response
    )
    return {
        "reply": response,
        "messages": [
            AIMessage(
                content=response["text"],
                id=response.get("message_id") or "reply:" + state["chat_event"],
            )
        ],
    }
