"""Exercise pre-verification conversation through Studio's real graph."""

import socket
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agents.collections.agent import build_graph
from agents.collections.chat_memory import ChatMemory
from agents.collections.memory import SQLiteRepository
from agents.collections.runtime import AppContext


@pytest.fixture
def chat(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("Offline test"))
    ctx = AppContext(
        SQLiteRepository(tmp_path / "chat.db"), datetime.fromisoformat("2026-10-19T09:00:00-05:00")
    )
    graph = build_graph(studio=True, checkpointer=InMemorySaver())
    sequence = 0

    def send(text, event=None):
        nonlocal sequence
        sequence += 1
        return graph.invoke(
            {"messages": [HumanMessage(content=text, id=event or str(sequence))]},
            config={"configurable": {"thread_id": "onboarding"}},
            context=ctx,
        )

    return send, ctx


def test_screenshot_sequence_acknowledges_refusal_then_payment_intent(chat, monkeypatch):
    send, ctx = chat
    original = ChatMemory.customer_history
    monkeypatch.setattr(
        ChatMemory, "customer_history", lambda *args: pytest.fail("No unverified history")
    )
    replies = [
        send(text)["reply"]
        for text in [
            "que hay",
            "no quiero hacer ni mierda",
            "que no quiero hacer nada",
            "les voy a opagar",
        ]
    ]
    assert len({r["text"] for r in replies}) == 4
    assert "No insistiré" in replies[1]["text"]
    assert "pausa" in replies[2]["text"]
    assert "verificar la identidad" in replies[3]["text"]
    assert all(r["stage"] == "awaiting_customer" and not r["finished"] for r in replies)
    assert all("600,000" not in r["text"] for r in replies)
    assert ctx.repository.contacts_for("demo-001") == []
    assert not ctx.repository.agreements()
    send("1000000001")
    monkeypatch.setattr(ChatMemory, "customer_history", original)
    assert send("verificar 1111")["reply"]["stage"] == "ready"
    send("quiero 3 cuotas desde mañana")
    assert send("confirmo")["reply"]["stage"] == "agreed"


def test_paused_onboarding_persists_without_checkpoint_and_replays(chat):
    send, ctx = chat
    result = send("no quiero hacer nada", "decline")
    assert send("no quiero hacer nada", "decline")["reply"] == result["reply"]
    assert len(ChatMemory(ctx.repository).binding("onboarding")["onboarding_history"]) == 1
    graph = build_graph(studio=True)
    result = graph.invoke(
        {"messages": [HumanMessage(content="hola", id="later")]},
        config={"configurable": {"thread_id": "onboarding"}},
        context=ctx,
    )
    assert "Seguimos en pausa" in result["reply"]["text"]
    assert "1000000001" not in result["reply"]["text"]


def test_conversation_does_not_spend_identity_attempts(chat):
    send, ctx = chat
    send("1000000001")
    for text in [
        "no quiero hacer nada",
        "que mal servicio",
        "por qué necesitas verificar",
        "les voy a opagar",
    ]:
        assert send(text)["reply"]["stage"] == "awaiting_identity"
    binding = ChatMemory(ctx.repository).binding("onboarding")
    assert binding["attempts"] == 0
    assert not binding["verified"]
    for i in range(3):
        result = send("verificar 0000")
    assert result["reply"]["stage"] == "closed"


@pytest.mark.parametrize("selected", [False, True])
def test_stop_before_verification_stays_closed(chat, selected):
    send, ctx = chat
    if selected:
        send("1000000001")
    assert send("No me llames más, ¿entendido?")["reply"]["stage"] == "closed"
    assert send("quiero pagar")["reply"]["stage"] == "closed"
    assert not ctx.repository.agreements()


def test_structured_interpreter_has_no_identity_authority(chat, monkeypatch):
    send, ctx = chat
    captured = []

    class Model:
        def __init__(self, **kwargs):
            assert kwargs["model"].startswith("claude-haiku")

        def with_structured_output(self, schema):
            return self

        def invoke(self, messages):
            captured.append(messages[-1][1])
            return {"intent": "willing"}

    monkeypatch.setitem(sys.modules, "langchain_anthropic", SimpleNamespace(ChatAnthropic=Model))
    ctx.provider = "anthropic"
    result = send("quiero pagar 123 pesos")
    assert "123" not in captured[0]
    assert result["reply"]["stage"] == "awaiting_customer"
    binding = ChatMemory(ctx.repository).binding("onboarding")
    assert not binding["verified"] and binding["account_id"] is None
    assert not ctx.repository.agreements()


def test_model_failure_keeps_thread_open_and_reports_error(chat, monkeypatch):
    send, ctx = chat
    from agents.collections import onboarding

    monkeypatch.setattr(
        onboarding, "classify_onboarding", lambda *args: (_ for _ in ()).throw(TimeoutError())
    )
    result = send("que hay")["reply"]
    assert result["interpretation_status"] == "model_error"
    assert not result["finished"]
    assert ChatMemory(ctx.repository).binding("onboarding")["attempts"] == 0
