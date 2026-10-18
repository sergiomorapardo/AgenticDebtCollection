"""Natural customer-facing entry copy, with tester instructions outside the chat."""

import re
from datetime import datetime

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agents.collections.agent import build_graph
from agents.collections.chat_memory import ChatMemory
from agents.collections.memory import SQLiteRepository
from agents.collections.runtime import AppContext


def test_entry_avoids_test_script_and_accepts_plain_verification_code(tmp_path):
    ctx = AppContext(
        SQLiteRepository(tmp_path / "entry.db"), datetime.fromisoformat("2026-10-19T09:00:00-05:00")
    )
    graph = build_graph(studio=True, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "entry"}}
    results = []
    for i, text in enumerate(
        ["hola", "no quiero", "quiero continuar", "1000000001", "0000", "1111"]
    ):
        result = graph.invoke(
            {"messages": [HumanMessage(content=text, id=str(i))]}, config=config, context=ctx
        )
        results.append(result["reply"])
    for reply in results:
        lower = reply["text"].lower()
        assert not any(
            word in lower
            for word in [
                "simulaci",
                "fictici",
                "1000000001",
                "1000000002",
                "1111",
            ]
        )
        assert not re.search(r"\b(demo|thread)\b", lower)
    assert "asistente virtual" in results[0]["text"]
    assert results[4]["stage"] == "awaiting_identity"
    assert results[-1]["stage"] == "ready"
    assert "600,000" in results[-1]["text"]
    assert all("600,000" not in reply["text"] for reply in results[:-1])
    assert ChatMemory(ctx.repository).binding("entry")["attempts"] == 1
    assert not ctx.repository.agreements()
