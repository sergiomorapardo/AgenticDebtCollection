"""Prepara una promesa por el saldo completo, aún sin registrar."""

from langgraph.runtime import Runtime

from agents.collections.nodes.negotiation.tools import prepare_offer
from agents.collections.runtime import AppContext, resolve_context
from agents.collections.state import State


def prepare_promise(state: State, runtime: Runtime[AppContext]) -> dict:
    return prepare_offer(state, resolve_context(runtime), negotiation=False)
