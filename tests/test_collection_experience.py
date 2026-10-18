"""In-character collection outcomes preserve exact business effects and limits."""

import re
from datetime import datetime

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agents.collections.agent import build_graph
from agents.collections.memory import SQLiteRepository
from agents.collections.runtime import AppContext


@pytest.mark.parametrize(
    "turns,stage,agreements",
    [
        (["quiero 3 cuotas desde mañana", "confirmo"], "agreed", 1),
        (["no quiero pagar", "no voy a pagar"], "closed", 0),
        (["quiero un asesor"], "handoff", 0),
        (["ya pagué"], "handoff", 0),
        (["quiero un descuento"], "handoff", 0),
        (["sin ingresos", "cero"], "handoff", 0),
        (["terminar"], "closed", 0),
    ],
)
def test_collection_outcomes_use_natural_copy_without_changing_business_effects(
    tmp_path, monkeypatch, turns, stage, agreements
):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    ctx = AppContext(
        SQLiteRepository(tmp_path / "experience.db"),
        datetime.fromisoformat("2026-10-19T09:00:00-05:00"),
    )
    graph = build_graph(studio=True, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "experience"}}
    for index, text in enumerate(["1000000001", "1111", *turns]):
        result = graph.invoke(
            {"messages": [HumanMessage(content=text, id=str(index))]}, config=config, context=ctx
        )
        response = result["reply"]["text"]
        assert not re.search(
            r"simulad|simulaci|fictici|\bdemo\b|\bthread\b", response, re.IGNORECASE
        )
    assert result["reply"]["stage"] == stage
    assert len(ctx.repository.agreements()) == agreements
    assert ctx.repository.get_account("demo-001").balance_cop == 600000
    if agreements:
        assert "promesa de pago" in response and "pendiente" in response
    if turns == ["quiero un asesor"]:
        assert "No puedo transferirte" in response
