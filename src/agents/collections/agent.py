"""Workflow por turnos del simulador local de cobranza."""

from langgraph.graph import END, START, StateGraph

from agents.collections.dialogue import plan_dialogue
from agents.collections.nodes.close.node import close_session
from agents.collections.nodes.commitment.node import save_commitment
from agents.collections.nodes.confirmation.node import validate_confirmation
from agents.collections.nodes.handoff.node import prepare_handoff
from agents.collections.nodes.intake.node import prepare_turn
from agents.collections.nodes.interpret.node import interpret_turn
from agents.collections.nodes.negotiation.node import prepare_payment_plans
from agents.collections.nodes.promise.node import prepare_promise
from agents.collections.nodes.response.node import compose_response
from agents.collections.routes.after_confirmation.agent import route_after_confirmation
from agents.collections.routes.after_intake.agent import route_after_intake
from agents.collections.routes.intent.agent import route_intent
from agents.collections.state import InputState, State


def build_graph(*, studio=False, checkpointer=None):
    """Construye el workflow sin credenciales, red ni efectos de negocio."""
    # Las dependencias Python se inyectan al invocar; no son contexto JSON público.
    if studio:
        from agents.collections.chat import (
            ChatInput,
            ChatState,
            assistant_message,
            human_input,
            identify_customer,
            recover_history,
            studio_intake,
            verify_identity,
        )

        builder = StateGraph(ChatState, input_schema=ChatInput)
        for name, node in {
            "human_input": human_input,
            "identify_customer": identify_customer,
            "verify_identity": verify_identity,
            "recover_history": recover_history,
            "assistant_message": assistant_message,
        }.items():
            builder.add_node(name, node)
        builder.add_edge(START, "human_input")
        builder.add_conditional_edges(
            "human_input",
            lambda state: state["chat_route"],
            {
                name: name
                for name in (
                    "identify_customer",
                    "verify_identity",
                    "recover_history",
                    "intake",
                    "assistant_message",
                )
            },
        )
        builder.add_edge("identify_customer", "assistant_message")
        for name in ("verify_identity", "recover_history"):
            destinations = (
                ("recover_history", "assistant_message")
                if name == "verify_identity"
                else ("intake", "assistant_message")
            )
            builder.add_conditional_edges(
                name, lambda state: state["chat_route"], {target: target for target in destinations}
            )
        builder.add_edge("response", "assistant_message")
        builder.add_edge("assistant_message", END)
    else:
        builder = StateGraph(State, input_schema=InputState)
        builder.add_edge(START, "intake")
        builder.add_edge("response", END)
    builder.add_node("intake", studio_intake if studio else prepare_turn)
    builder.add_node("interpret", interpret_turn)
    builder.add_node("dialogue_strategy", plan_dialogue)
    builder.add_edge("interpret", "dialogue_strategy")
    builder.add_node("promise", prepare_promise)
    builder.add_node("negotiation", prepare_payment_plans)
    builder.add_node("confirmation", validate_confirmation)
    builder.add_node("commitment", save_commitment)
    builder.add_node("handoff", prepare_handoff)
    builder.add_node("response", compose_response)
    builder.add_node("close", close_session)
    builder.add_conditional_edges(
        "intake",
        route_after_intake,
        {
            "interpret": "interpret",
            "response": "response",
            "close": "close",
        },
    )
    builder.add_conditional_edges(
        "dialogue_strategy",
        route_intent,
        {
            "promise": "promise",
            "negotiation": "negotiation",
            "confirmation": "confirmation",
            "handoff": "handoff",
            "close": "close",
            "response": "response",
        },
    )
    builder.add_conditional_edges(
        "confirmation",
        route_after_confirmation,
        {
            "commitment": "commitment",
            "response": "response",
        },
    )
    for name in ("promise", "negotiation", "commitment", "handoff", "close"):
        builder.add_edge(name, "response")
    # Studio administra checkpoints; el adaptador CLI persistente pertenece a la etapa 1.
    return builder.compile(checkpointer=checkpointer)


agent = build_graph()
