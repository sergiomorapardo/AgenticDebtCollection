"""Bounded wording, grounding, retries, and policy isolation without paid calls."""

from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agents.collections import wording
from agents.collections.agent import build_graph
from agents.collections.memory import SQLiteRepository
from agents.collections.runtime import AppContext


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    ctx = AppContext(
        SQLiteRepository(tmp_path / "wording.sqlite3"),
        datetime.fromisoformat("2026-10-19T09:00:00-05:00"),
        provider="anthropic",
    )
    return ctx, SimpleNamespace(context=ctx)


def state():
    return {
        "session": {"stage": "awaiting_terms", "identity_verified": True, "history": []},
        "text": "perdí mi trabajo",
        "interpretation": {"intent": "hardship"},
        "response_text": "¿Qué monto mensual podrías pagar sin afectar tus gastos esenciales?",
    }


def test_grounded_reply_is_contextual_and_never_writes_agreements(setup, monkeypatch):
    ctx, runtime = setup
    observed = []
    candidate = "¿Qué cantidad podrías destinar cada mes sin comprometer tus gastos esenciales?"

    def provider(context, schema, system, payload):
        observed.append((schema, payload))
        return schema.model_validate(
            {"text": candidate}
            if schema is wording.ReplyDraft
            else {"preserves_next_step": True, "no_new_claims": True, "respectful": True}
        )

    monkeypatch.setattr(wording, "_request", provider)
    current = state()
    current.update(wording.prepare_reply(current, runtime))
    current.update(wording.draft_reply(current, runtime))
    current.update(wording.review_reply(current, runtime))
    assert current["response_text"] == candidate
    assert current["wording_status"] == "validated"
    assert set(observed[0][1]) == {
        "reference",
        "current_message",
        "recent_dialogue",
        "intent",
        "previous_draft_rejected",
    }
    assert observed[0][1]["current_message"] == "perdí mi trabajo"
    assert not ctx.repository.agreements()


@pytest.mark.parametrize(
    "candidate", ["Te doy un descuento del 50%", "Te demandaremos", "Paga doscientos mil mañana"]
)
def test_rejected_wording_retries_once_then_uses_reference(setup, monkeypatch, candidate):
    _, runtime = setup

    def provider(context, schema, system, payload):
        return schema.model_validate(
            {"text": candidate}
            if schema is wording.ReplyDraft
            else {"preserves_next_step": False, "no_new_claims": False, "respectful": False}
        )

    monkeypatch.setattr(wording, "_request", provider)
    current = state()
    reference = current["response_text"]
    current.update(wording.prepare_reply(current, runtime))
    for expected in ("draft_reply", "response"):
        current.update(wording.draft_reply(current, runtime))
        current.update(wording.review_reply(current, runtime))
        assert current["wording_route"] == expected
    assert current["wording_attempts"] == 2
    assert current["response_text"] == reference
    assert current["wording_status"] == "rejected"


def test_model_error_is_observable_and_does_not_loop(setup, monkeypatch):
    _, runtime = setup

    def unavailable(*args):
        raise TimeoutError("synthetic error")

    monkeypatch.setattr(wording, "_request", unavailable)
    current = state()
    current.update(wording.prepare_reply(current, runtime))
    current.update(wording.draft_reply(current, runtime))
    current.update(wording.review_reply(current, runtime))
    assert current["wording_status"] == "model_error"
    assert current["wording_attempts"] == 1
    assert current["wording_route"] == "response"


@pytest.mark.parametrize(
    "patch",
    [
        {"draft_offer": {"id": "offer"}},
        {"commit_requested": True},
        {"replay": True},
        {"response_text": "¿Confirmas 200,000 COP el 2026-10-20?"},
        {"interpretation": {}},
    ],
)
def test_financial_and_transactional_replies_bypass_generation(setup, patch):
    _, runtime = setup
    current = {**state(), **patch, "wording_attempts": 9}
    reset = wording.prepare_reply(current, runtime)
    assert reset["wording_route"] == "response"
    assert reset["wording_status"] == "deterministic"
    assert reset["wording_attempts"] == 0


def test_studio_retry_loop_is_bounded_and_next_turn_resets(setup, monkeypatch):
    ctx, _ = setup
    from agents.collections import semantic

    understand = semantic.understand_turn
    monkeypatch.setattr(
        semantic,
        "understand_turn",
        lambda text, **kwargs: understand(
            text, **{**kwargs, "ctx": replace(kwargs["ctx"], provider="mock")}
        ),
    )
    calls = []

    def provider(context, schema, system, payload):
        calls.append(schema)
        return schema.model_validate({"text": "Debes pagar 999 pesos"})

    monkeypatch.setattr(wording, "_request", provider)
    graph = build_graph(studio=True, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "test"}}
    for index, text in enumerate(
        ["1000000001", "verificar 1111", "perdí mi trabajo", "no tengo ingresos"]
    ):
        result = graph.invoke(
            {"messages": [HumanMessage(content=text, id=str(index))]}, config=config, context=ctx
        )
    assert result["wording_attempts"] == 2
    assert result["reply"]["wording_status"] == "rejected"
    assert "999" not in result["reply"]["text"]
    assert calls == [wording.ReplyDraft] * 4
    assert not ctx.repository.agreements()
    edges = {(edge.source, edge.target) for edge in graph.get_graph().edges}
    assert ("review_reply", "draft_reply") in edges


@pytest.mark.parametrize(
    "text,stage,output",
    [
        ("mañana", "awaiting_terms", {"intent": "unclear", "payment_date": "2026-10-20"}),
        (
            "confirmo",
            "awaiting_confirmation",
            {"intent": "accept_offer", "acceptance": "unconditional_current_offer"},
        ),
        ("terminar", "ready", {"intent": "end"}),
    ],
)
def test_exact_dates_confirmations_and_stops_always_reach_the_model(
    monkeypatch, text, stage, output
):
    import json
    import sys
    from datetime import date

    from agents.collections.semantic import TurnUnderstanding, understand_turn

    calls = []

    class FakeModel:
        def __init__(self, **kwargs):
            pass

        def with_structured_output(self, schema):
            assert schema is TurnUnderstanding
            return self

        def invoke(self, messages):
            calls.append(messages)
            return output

    monkeypatch.setitem(
        sys.modules, "langchain_anthropic", SimpleNamespace(ChatAnthropic=FakeModel)
    )
    result = understand_turn(
        text,
        today=date(2026, 10, 19),
        ctx=SimpleNamespace(provider="anthropic", model="test-model"),
        stage=stage,
    )
    assert len(calls) == 1 and json.loads(calls[0][1][1])["current_message"] == text
    assert result.status == "classified"
    assert result.turn.intent == output["intent"]
    if "payment_date" in output:
        assert result.turn.payment_date.isoformat() == output["payment_date"]


def test_writer_receives_full_current_message_and_bounded_private_history():
    current = state()
    current["wording_reference"] = current["response_text"]
    current["text"] = "x" * 700 + " pero no quiero seguir; cédula 123456789"
    current["session"]["history"] = [
        {"user": "1111 " + "x" * 700, "assistant": "Contacto test@example.com"}
    ] * 8
    payload = wording._dialogue_payload(current)
    assert "pero no quiero seguir" in payload["current_message"]
    assert "123456789" not in str(payload) and "1111" not in str(payload)
    assert "test@example.com" not in str(payload)
    assert len(payload["recent_dialogue"]) == 4
    assert len(payload["recent_dialogue"][0]["user"]) == 600
