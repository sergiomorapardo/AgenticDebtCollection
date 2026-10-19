"""Contextual continuation regression: synthetic data, no external model calls."""

import json
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agents.collections.agent import build_graph
from agents.collections.chat_memory import ChatMemory
from agents.collections.continuation import interpret_continuation
from agents.collections.conversation import LEGACY_CLOSE
from agents.collections.memory import SQLiteRepository
from agents.collections.runtime import AppContext
from agents.collections.semantic import understand_turn
from agents.collections.semantic_state import bind_understanding


@pytest.fixture
def continuation(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    ctx = AppContext(
        SQLiteRepository(tmp_path / "continuation.db"),
        datetime.fromisoformat("2026-10-19T09:00:00-05:00"),
        provider="mock",
    )
    graph = build_graph(studio=True, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "continuation-regression"}}

    def send(text, event):
        return graph.invoke(
            {"messages": [HumanMessage(content=text, id=event)]}, config=config, context=ctx
        )

    send("123456789", "identify")
    send("1111", "verify")
    session_id = ChatMemory(ctx.repository).binding("continuation-regression")["session_id"]
    session = ctx.repository.get_session(session_id)
    session.update(stage="closed", pending_offer_id=None)
    session["history"].append(
        {
            "event_id": "historical-bug",
            "user": "ayudame a programar en python",
            "assistant": LEGACY_CLOSE,
        }
    )
    with ctx.repository.connection() as db:
        db.execute("UPDATE sessions SET data=? WHERE id=?", (json.dumps(session), session_id))
    send("¿qué pasó?", "question")
    return send, ctx, session_id


def model_stub(monkeypatch, result, captured):
    class Model:
        def __init__(self, **kwargs):
            assert "haiku" in kwargs["model"]

        def with_structured_output(self, schema):
            return self

        def invoke(self, messages):
            captured.append(json.loads(messages[-1][1]))
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setitem(sys.modules, "langchain_anthropic", SimpleNamespace(ChatAnthropic=Model))


@pytest.mark.parametrize("answer", ["sigue", "dale", "sí, adelante"])
def test_natural_continuation_resumes_only_the_conversation(continuation, answer):
    send, ctx, session_id = continuation
    result = send(answer, "resume")
    assert result["reply"]["stage"] == "ready"
    assert not ctx.repository.agreements()
    assert ctx.repository.get_session(session_id)["pending_offer_id"] is None
    assert ctx.repository.get_account("demo-001").balance_cop == 600000
    assert send(answer, "resume")["reply"] == result["reply"]


def test_real_provider_interprets_paraphrase_with_context_not_allowlist(continuation, monkeypatch):
    send, ctx, session_id = continuation
    captured = []
    model_stub(monkeypatch, {"intent": "continue"}, captured)
    ctx.provider = "anthropic"
    result = send("bueno, cuéntame qué opciones tengo entonces", "resume")
    assert result["reply"]["stage"] == "ready"
    assert len(captured) == 1
    assert captured[0]["current_message"] == "bueno, cuéntame qué opciones tengo entonces"
    assert "¿Quieres que sigamos" in json.dumps(captured[0], ensure_ascii=False)
    assert not ctx.repository.agreements()
    assert ctx.repository.get_session(session_id)["pending_offer_id"] is None
    assert send("bueno, cuéntame qué opciones tengo entonces", "resume")["reply"] == result["reply"]
    assert len(captured) == 1


@pytest.mark.parametrize(
    "result", [{"intent": "unclear"}, {"intent": "confirm_payment"}, TimeoutError()]
)
def test_ambiguous_or_failed_interpretation_never_resumes(continuation, monkeypatch, result):
    send, ctx, session_id = continuation
    captured = []
    model_stub(monkeypatch, result, captured)
    ctx.provider = "anthropic"
    response = send("¿qué pasa si digo sigue?", "ambiguous")
    assert response["reply"]["stage"] == "closed"
    assert ctx.repository.get_session(session_id).get("recovered_from") is None
    assert not ctx.repository.agreements()
    if isinstance(result, dict) and result.get("intent") == "unclear":
        assert "¿Quieres que sigamos" in response["reply"]["text"]
    else:
        assert response["reply"]["interpretation_status"] == "model_error"
        assert "intentarlo de nuevo" in response["reply"]["text"]
    assert len(captured) == 1
    assert send("¿qué pasa si digo sigue?", "ambiguous")["reply"] == response["reply"]
    assert len(captured) == 1


@pytest.mark.parametrize("intent", ["end", "decline"])
def test_semantic_decline_is_respected_on_later_turns(continuation, monkeypatch, intent):
    send, ctx, session_id = continuation
    captured = []
    model_stub(monkeypatch, {"intent": intent}, captured)
    ctx.provider = "anthropic"
    assert send("mejor lo dejamos aquí por hoy", "decline")["reply"]["stage"] == "closed"
    model_stub(monkeypatch, {"intent": "continue"}, captured)
    assert send("sigue", "later")["reply"]["stage"] == "closed"
    assert len(captured) == 2
    assert ctx.repository.get_session(session_id)["close_reason"] == "customer_request"


@pytest.mark.parametrize("stage", ["closed", "agreed", "handoff"])
def test_legitimate_terminal_outcomes_classify_once_without_reopening(
    continuation, monkeypatch, stage
):
    send, ctx, session_id = continuation
    session = ctx.repository.get_session(session_id)
    session.update(stage=stage, close_reason="customer_request")
    with ctx.repository.connection() as db:
        db.execute("UPDATE sessions SET data=? WHERE id=?", (json.dumps(session), session_id))
    captured = []
    model_stub(monkeypatch, {"intent": "continue"}, captured)
    ctx.provider = "anthropic"
    assert send("sigue", "later")["reply"]["stage"] == stage
    assert len(captured) == 1
    assert not ctx.repository.agreements()
    assert send("sigue", "later")["reply"]["stage"] == stage
    assert len(captured) == 1


def test_redacted_context_is_bounded(continuation, monkeypatch):
    _, ctx, _ = continuation
    captured = []
    model_stub(monkeypatch, {"intent": "continue"}, captured)
    ctx.provider = "anthropic"
    secret = "123456789 1111 test@example.com https://example.com/account"
    history = [HumanMessage(content=secret + "x" * 700), AIMessage(content="¿Quieres seguir?")] * 8
    state = {"event_id": "bounded-context", "text": "sigue"}
    meaning = understand_turn(
        state["text"],
        ctx=ctx,
        today=ctx.today,
        stage="closed",
        history=history,
        context={"recoverable_misclosure": True},
    )
    state["semantic_result"] = bind_understanding(state, meaning)
    assert interpret_continuation(state, {}, ctx) == ("resume", "classified")
    assert len(captured) == 1
    serialized = json.dumps(captured[0])
    assert not any(
        value in serialized for value in ["123456789", "1111", "test@example.com", "https://"]
    )
    assert len(captured[0]["recent_dialogue"]) <= 4
    assert all(len(turn["text"]) <= 600 for turn in captured[0]["recent_dialogue"])


def test_continuation_without_bound_meaning_never_calls_a_model(continuation, monkeypatch):
    _, ctx, _ = continuation
    captured = []
    model_stub(monkeypatch, {"intent": "continue"}, captured)
    ctx.provider = "anthropic"
    assert interpret_continuation({"text": "sigue"}, {}, ctx) == ("unclear", "model_error")
    assert captured == []


def test_current_message_keeps_late_negation_beyond_history_excerpt_limit(
    continuation, monkeypatch
):
    send, ctx, _ = continuation
    captured = []
    model_stub(monkeypatch, {"intent": "decline"}, captured)
    ctx.provider = "anthropic"
    ending = "Pero por ahora prefiero no retomar esta conversación."
    text = "Sí, quiero continuar. " + "Necesito explicar lo que pasó. " * 30 + ending
    assert 600 < len(text) < 4000
    result = send(text, "long-answer")
    assert captured[0]["current_message"].endswith(ending)
    assert result["reply"]["stage"] == "closed"
    assert not ctx.repository.agreements()
