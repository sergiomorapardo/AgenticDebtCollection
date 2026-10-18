"""Calcula un plan elegible mediante reglas deterministas."""

from langgraph.runtime import Runtime

from agents.collections.runtime import AppContext, resolve_context
from agents.collections.state import State

from .tools import prepare_offer


def prepare_payment_plans(state: State, runtime: Runtime[AppContext]) -> dict:
    return prepare_offer(state, resolve_context(runtime), negotiation=True)
