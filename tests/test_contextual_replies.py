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
        "mentions_internal_marker",
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


@pytest.mark.parametrize("marker", ["[number]", "[redacted]", "<number_0>"])
def test_reviewed_wording_rejects_internal_markers_before_model_review(setup, monkeypatch, marker):
    _, runtime = setup

    def provider(ctx, schema, system, payload):
        assert schema is wording.ReplyDraft
        return wording.ReplyDraft(text=f"Mencionaste {marker}; ¿qué necesitas aclarar?")

    monkeypatch.setattr(wording, "_request", provider)
    current = state()
    reference = current["response_text"]
    current.update(wording.prepare_reply(current, runtime))
    for _ in range(2):
        current.update(wording.draft_reply(current, runtime))
        current.update(wording.review_reply(current, runtime))
    assert current["response_text"] == reference
    assert current["wording_status"] == "rejected"


def test_wording_payload_does_not_recycle_historical_markers_or_identifiers():
    import json

    from agents.collections.customer_text import contains_internal_marker

    current = state()
    current.update(wording_reference=current["response_text"], text="cuotas de 1 peso, [number]")
    current["session"]["history"] = [
        {
            "user": "123456789 test@example.com",
            "assistant": "de [number] pesos, <number_0> [redacted]",
        }
    ]
    encoded = json.dumps(wording._dialogue_payload(current), ensure_ascii=False)
    assert not contains_internal_marker(encoded)
    assert "123456789" not in encoded and "test@example.com" not in encoded
    assert "una cifra" in encoded


@pytest.mark.parametrize(
    "terms,expected",
    [
        ({"intent": "negotiate", "installments": 1000}, "1,000 cuotas"),
        ({"intent": "negotiate", "amount_cop": 1, "frequency": "monthly"}, "1 COP"),
    ],
)
def test_invalid_plan_is_explained_by_backend_without_wording_or_agreement(
    setup, monkeypatch, terms, expected
):
    from agents.collections import semantic
    from agents.collections.chat_memory import ChatMemory
    from agents.collections.semantic import TurnResult, TurnUnderstanding

    ctx, _ = setup
    ctx.provider = "mock"
    graph = build_graph(studio=True, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "invalid-plan"}}

    def send(text, event):
        return graph.invoke(
            {"messages": [HumanMessage(content=text, id=event)]}, config=config, context=ctx
        )

    send("123456789", "identify")
    send("1111", "verify")
    send("3 cuotas mañana", "old-offer")
    monkeypatch.setattr(
        semantic,
        "understand_turn",
        lambda *args, **kwargs: TurnResult(TurnUnderstanding(**terms), "classified"),
    )
    ctx.provider = "anthropic"
    monkeypatch.setattr(
        wording, "_request", lambda *args: pytest.fail("Financial boundary must not be rewritten")
    )
    result = send("An invalid plan interpreted by the model", "invalid")
    assert expected in result["reply"]["text"]
    assert "3 cuotas" in result["reply"]["text"] and "200,000 COP" in result["reply"]["text"]
    assert result["reply"]["offer_id"] is None
    assert result["reply"]["stage"] == ("handoff" if terms.get("installments") else "ready")
    assert not ctx.repository.agreements()
    session_id = ChatMemory(ctx.repository).binding("invalid-plan")["session_id"]
    assert ctx.repository.get_session(session_id)["draft_terms"] == {}
    assert send("An invalid plan interpreted by the model", "invalid")["reply"] == result["reply"]


@pytest.mark.parametrize("frequency", ["unspecified", "one_off"])
def test_small_amount_without_monthly_recurrence_is_not_rejected_as_installment(
    setup, monkeypatch, frequency
):
    from agents.collections import semantic
    from agents.collections.chat_memory import ChatMemory
    from agents.collections.semantic import TurnResult, TurnUnderstanding

    ctx, _ = setup
    ctx.provider = "mock"
    graph = build_graph(studio=True, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "ambiguous-amount"}}
    for index, text in enumerate(["123456789", "1111", "qué opciones tengo"]):
        graph.invoke(
            {"messages": [HumanMessage(content=text, id=str(index))]}, config=config, context=ctx
        )
    monkeypatch.setattr(
        semantic,
        "understand_turn",
        lambda *args, **kwargs: TurnResult(
            TurnUnderstanding(intent="negotiate", amount_cop=1000, frequency=frequency),
            "classified",
        ),
    )
    result = graph.invoke(
        {"messages": [HumanMessage(content="Un abono pequeño", id="amount")]},
        config=config,
        context=ctx,
    )
    assert "No puedo ofrecer cuotas" not in result["reply"]["text"]
    assert "único abono" in result["reply"]["text"]
    assert result["reply"]["offer_id"] is None
    session_id = ChatMemory(ctx.repository).binding("ambiguous-amount")["session_id"]
    assert ctx.repository.get_session(session_id)["dialogue"]["awaiting"] == "frequency"
    assert not ctx.repository.agreements()
