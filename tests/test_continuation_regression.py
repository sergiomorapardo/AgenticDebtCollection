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
def test_customer_can_change_their_mind_after_declining(continuation, monkeypatch, intent):
    send, ctx, session_id = continuation
    captured = []
    model_stub(monkeypatch, {"intent": intent}, captured)
    ctx.provider = "anthropic"
    assert send("mejor lo dejamos aquí por hoy", "decline")["reply"]["stage"] == "closed"
    model_stub(monkeypatch, {"intent": "continue"}, captured)
    assert ctx.repository.get_session(session_id)["close_reason"] == "customer_request"
    assert send("sigue", "later")["reply"]["stage"] == "ready"
    assert len(captured) == 2
    assert ctx.repository.get_session(session_id)["close_reason"] is None
    assert not ctx.repository.agreements()


@pytest.mark.parametrize("stage", ["agreed", "handoff"])
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


@pytest.fixture
def refused_conversation(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    ctx = AppContext(
        SQLiteRepository(tmp_path / "refused.db"),
        datetime.fromisoformat("2026-10-19T09:00:00-05:00"),
    )
    graph = build_graph(studio=True, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "refused-conversation"}}

    def send(text, event):
        return graph.invoke(
            {"messages": [HumanMessage(content=text, id=event)]}, config=config, context=ctx
        )

    send("123456789", "identify")
    send("1111", "verify")
    send("no quiero pagar", "refuse-once")
    closed = send("no voy a pagar", "refuse-twice")
    assert closed["reply"]["stage"] == "closed"
    session_id = ChatMemory(ctx.repository).binding("refused-conversation")["session_id"]
    return send, ctx, session_id


def update_stored_session(ctx, session_id, **updates):
    session = ctx.repository.get_session(session_id)
    session.update(updates)
    with ctx.repository.connection() as db:
        db.execute("UPDATE sessions SET data=? WHERE id=?", (json.dumps(session), session_id))
    return session


def test_screenshot_refusal_then_dispute_receives_review_without_payment(refused_conversation):
    send, ctx, session_id = refused_conversation
    previous = ctx.repository.get_session(session_id)
    disputed = send("no reconozco esa deuda", "dispute")
    assert disputed["reply"]["stage"] == "handoff"
    assert "revisión" in disputed["reply"]["text"]
    assert "no retomaré" not in disputed["reply"]["text"]
    assert not ctx.repository.agreements()
    assert ctx.repository.get_account("demo-001").balance_cop == 600000
    current = ctx.repository.get_session(session_id)
    assert current["history"][: len(previous["history"])] == previous["history"]
    assert current["negotiations"] == previous["negotiations"]
    assert send("no reconozco esa deuda", "dispute")["reply"] == disputed["reply"]


def test_refusal_then_customer_resume_can_negotiate_once_and_replay(
    refused_conversation, monkeypatch
):
    send, ctx, session_id = refused_conversation
    previous = ctx.repository.get_session(session_id)
    update_stored_session(
        ctx,
        session_id,
        pending_offer_id="stale-offer",
        draft_terms={"installments": 2},
        clarifications=2,
    )
    from agents.collections import semantic
    from agents.collections.semantic import TurnResult, TurnUnderstanding

    understand = semantic.understand_turn
    resume_text = "quiero seguir revisando las opciones de pago"
    calls = []

    def semantic_resume(text, **kwargs):
        if text == resume_text:
            calls.append(text)
            return TurnResult(TurnUnderstanding(intent="continue"), "classified")
        return understand(text, **kwargs)

    monkeypatch.setattr(semantic, "understand_turn", semantic_resume)
    resumed = send(resume_text, "resume")
    assert resumed["reply"]["stage"] == "ready"
    current = ctx.repository.get_session(session_id)
    assert current["pending_offer_id"] is None and current["draft_terms"] == {}
    assert current["dialogue"]["refusals"] == 0 and current["clarifications"] == 0
    assert current["negotiations"] == previous["negotiations"]
    assert current["turns"] == previous["turns"] + 1
    assert current["history"][: len(previous["history"])] == previous["history"]
    assert send(resume_text, "resume")["reply"] == resumed["reply"]
    assert calls == [resume_text]
    assert ctx.repository.get_session(session_id)["turns"] == current["turns"]
    assert not ctx.repository.agreements()
    proposal = send("quiero tres cuotas desde mañana", "new-offer")
    assert proposal["reply"]["stage"] == "awaiting_confirmation"
    accepted = send("confirmo", "new-acceptance")
    assert accepted["reply"]["stage"] == "agreed" and len(ctx.repository.agreements()) == 1
    assert send("confirmo", "new-acceptance")["reply"] == accepted["reply"]
    assert len(ctx.repository.agreements()) == 1
    assert len(ctx.repository.contacts_for("demo-001")) == 1


@pytest.mark.parametrize(
    "intent,text",
    [("reported_payment", "yo ya pagué"), ("request_human", "necesito hablar con una persona")],
)
def test_closed_customer_requests_reach_review_without_creating_agreement(
    refused_conversation, monkeypatch, intent, text
):
    from agents.collections import semantic
    from agents.collections.semantic import TurnResult, TurnUnderstanding

    send, ctx, _ = refused_conversation
    monkeypatch.setattr(
        semantic,
        "understand_turn",
        lambda *args, **kwargs: TurnResult(TurnUnderstanding(intent=intent), "classified"),
    )
    result = send(text, "review-request")
    assert result["reply"]["stage"] == "handoff"
    assert not ctx.repository.agreements()
    assert ctx.repository.get_account("demo-001").balance_cop == 600000


@pytest.mark.parametrize(
    "intent,status",
    [
        ("unclear", "classified"),
        ("complaint", "classified"),
        ("clarify_process", "classified"),
        ("continue", "model_error"),
    ],
)
def test_ambiguity_complaint_and_model_failure_do_not_reopen(
    refused_conversation, monkeypatch, intent, status
):
    from agents.collections import semantic
    from agents.collections.semantic import TurnResult, TurnUnderstanding

    send, ctx, session_id = refused_conversation
    monkeypatch.setattr(
        semantic,
        "understand_turn",
        lambda *args, **kwargs: TurnResult(TurnUnderstanding(intent=intent), status),
    )
    result = send("¿y ahora qué?", "ambiguous-resume")
    assert result["reply"]["stage"] == "closed"
    assert ctx.repository.get_session(session_id)["close_reason"] == "repeated_refusal"
    assert not ctx.repository.agreements()


@pytest.mark.parametrize(
    "patch",
    [
        {"identity_verified": False},
        {"close_reason": "contact_blocked"},
        {"close_reason": "identity_attempts_exhausted"},
    ],
)
def test_authentication_and_contact_closures_cannot_resume(
    refused_conversation, monkeypatch, patch
):
    from agents.collections import semantic
    from agents.collections.semantic import TurnResult, TurnUnderstanding

    send, ctx, session_id = refused_conversation
    update_stored_session(ctx, session_id, **patch)
    monkeypatch.setattr(
        semantic,
        "understand_turn",
        lambda *args, **kwargs: TurnResult(TurnUnderstanding(intent="continue"), "classified"),
    )
    assert send("sí, retomemos", "blocked-resume")["reply"]["stage"] == "closed"
    assert not ctx.repository.agreements()


def test_resume_preserves_exhausted_turn_budget(refused_conversation, monkeypatch):
    send, ctx, session_id = refused_conversation
    update_stored_session(ctx, session_id, turns=20, negotiations=3)
    from agents.collections import semantic
    from agents.collections.semantic import TurnResult, TurnUnderstanding

    monkeypatch.setattr(
        semantic,
        "understand_turn",
        lambda *args, **kwargs: TurnResult(TurnUnderstanding(intent="continue"), "classified"),
    )
    result = send("quiero retomar la conversación", "budget-resume")
    assert result["reply"]["stage"] == "handoff"
    session = ctx.repository.get_session(session_id)
    assert session["turns"] == 21 and session["negotiations"] == 3
    assert not ctx.repository.agreements()


def test_reopened_studio_menu_waits_for_a_choice_without_repeating(
    refused_conversation, monkeypatch
):
    send, ctx, session_id = refused_conversation
    captured = []
    ctx.provider = "anthropic"
    model_stub(monkeypatch, {"intent": "continue"}, captured)
    menu = send("dale", "resume-menu")
    assert "600,000 COP" in menu["reply"]["text"]
    # Keep the contextual writer deterministic here; semantic calls still use the model stub.
    monkeypatch.setattr(
        "agents.collections.wording._request",
        lambda *args: (_ for _ in ()).throw(ValueError("writer unavailable")),
    )
    for i, message in enumerate(("bueno", "sí, esa")):
        result = send(message, f"ack-{i}")
        assert "Cuál de las opciones" in result["reply"]["text"]
        assert result["reply"]["stage"] == "ready"
        assert result["reply"]["offer_id"] is None
        assert ctx.repository.get_session(session_id)["dialogue"]["awaiting"] == "option"
    assert len(captured) == 3
    assert send("sí, esa", "ack-1")["reply"] == result["reply"]
    assert len(captured) == 3
    model_stub(
        monkeypatch,
        {
            "intent": "negotiate",
            "installments": 3,
            "installments_evidence": {"quote": "tres"},
            "payment_date": "2026-10-20",
        },
        captured,
    )
    proposal = send("la de tres, empezando mañana", "select-plan")
    assert proposal["reply"]["stage"] == "awaiting_confirmation"
    assert "200,000 COP" in proposal["reply"]["text"]
    assert not ctx.repository.agreements()


@pytest.mark.parametrize("text", ["ok, gracias", "dale, gracias", "gracias, hasta luego"])
def test_polite_closure_acknowledgement_does_not_reopen(refused_conversation, monkeypatch, text):
    send, ctx, session_id = refused_conversation
    captured = []
    model_stub(monkeypatch, {"intent": "unclear"}, captured)
    ctx.provider = "anthropic"
    result = send(text, "acknowledge-closure")
    assert len(captured) == 1
    assert result["reply"]["stage"] == "closed"
    assert ctx.repository.get_session(session_id)["close_reason"] == "repeated_refusal"
    assert not ctx.repository.agreements()


@pytest.fixture
def date_conversation(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")

    def create(day="2026-10-19"):
        ctx = AppContext(
            SQLiteRepository(tmp_path / (day + ".db")),
            datetime.fromisoformat(day + "T09:00:00-05:00"),
            provider="mock",
        )
        graph = build_graph(studio=True, checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "date-dialogue"}}
        sequence = 0

        def send(text):
            nonlocal sequence
            sequence += 1
            return graph.invoke(
                {"messages": [HumanMessage(content=text, id=str(sequence))]},
                config=config,
                context=ctx,
            )["reply"]

        send("123456789")
        send("1111")
        session_id = ChatMemory(ctx.repository).binding("date-dialogue")["session_id"]
        return send, ctx, session_id

    return create


@pytest.mark.parametrize(
    "answer,expected",
    [
        ("el viernes", "2026-10-23"),
        ("el 20", "2026-10-20"),
        ("20", "2026-10-20"),
        ("el veinte", "2026-10-20"),
    ],
)
def test_date_only_answer_preserves_plan_and_requires_full_offer_confirmation(
    date_conversation, answer, expected
):
    send, ctx, sid = date_conversation()
    send("quiero 3 cuotas")
    reply = send(answer)
    assert reply["stage"] == "awaiting_confirmation"
    assert expected in reply["text"] and "200,000 COP" in reply["text"]
    assert "Confirmas" in reply["text"]
    assert ctx.repository.get_session(sid)["draft_terms"]["installments"] == 3
    assert not ctx.repository.agreements()
    assert send("confirmo")["stage"] == "agreed"
    assert len(ctx.repository.agreements()) == 1


def test_same_weekday_clarifies_and_bare_confirmation_cannot_accept(date_conversation):
    send, ctx, sid = date_conversation("2026-10-23")
    send("quiero 3 cuotas")
    question = send("el viernes")
    assert question["stage"] == "awaiting_terms" and not question["offer_id"]
    assert "2026-10-23" in question["text"] and "2026-10-30" in question["text"]
    assert not send("confirmo")["offer_id"]
    assert not ctx.repository.agreements()
    proposal = send("el próximo")
    assert proposal["stage"] == "awaiting_confirmation"
    assert "2026-10-30" in proposal["text"] and "200,000 COP" in proposal["text"]
    assert ctx.repository.get_session(sid)["draft_terms"]["installments"] == 3
    assert not ctx.repository.agreements()


def test_day_outside_window_asks_month_then_year_without_losing_plan(date_conversation):
    send, ctx, sid = date_conversation("2026-10-26")
    send("quiero 3 cuotas")
    reply = send("el 20")
    assert "mes y año" in reply["text"]
    assert not reply["offer_id"]
    reply = send("noviembre")
    assert "qué año" in reply["text"]
    reply = send("2026")
    assert "entre hoy y siete días" in reply["text"]
    assert ctx.repository.get_session(sid)["draft_terms"]["installments"] == 3
    assert not ctx.repository.agreements()
    reply = send("el viernes")
    assert reply["stage"] == "awaiting_confirmation" and "2026-10-30" in reply["text"]


def test_ambiguous_new_date_invalidates_previous_offer_and_preserves_terms(date_conversation):
    send, ctx, sid = date_conversation()
    prior = send("quiero 3 cuotas desde 2026-10-20")
    assert prior["offer_id"]
    reply = send("el viernes o el 20")
    assert reply["offer_id"] is None and "más de una interpretación" in reply["text"]
    assert ctx.repository.get_session(sid)["draft_terms"].get("payment_date") is None
    assert send("confirmo")["offer_id"] is None
    assert not ctx.repository.agreements()


@pytest.mark.parametrize(
    "today,answer,expected",
    [("2026-12-30", "el 2", "2027-01-02"), ("2028-02-24", "29 de febrero", "2028-02-29")],
)
def test_date_resolution_handles_year_boundary_and_leap_day(
    date_conversation, today, answer, expected
):
    send, ctx, _ = date_conversation(today)
    send("quiero 3 cuotas")
    reply = send(answer)
    assert reply["stage"] == "awaiting_confirmation" and expected in reply["text"]
    assert not ctx.repository.agreements()


def test_impossible_calendar_date_asks_for_correction(date_conversation):
    send, ctx, sid = date_conversation("2026-02-24")
    send("quiero 3 cuotas")
    reply = send("31 de febrero")
    assert "no existe" in reply["text"] and reply["offer_id"] is None
    assert ctx.repository.get_session(sid)["draft_terms"]["installments"] == 3
    assert not ctx.repository.agreements()


@pytest.mark.parametrize("text", ["el primero de noviembre del 2026", "el 1 de noviembre del 2026"])
def test_natural_full_date_preserves_offer_and_requires_confirmation(date_conversation, text):
    send, ctx, sid = date_conversation("2026-10-26")
    send("quiero 3 cuotas")
    reply = send(text)
    assert reply["stage"] == "awaiting_confirmation" and "2026-11-01" in reply["text"]
    assert "200,000 COP" in reply["text"] and not ctx.repository.agreements()
    assert ctx.repository.get_session(sid)["draft_terms"]["installments"] == 3
    assert send("confirmo")["stage"] == "agreed"


def test_repeated_date_ambiguity_reaches_existing_handoff_limit(date_conversation):
    send, ctx, sid = date_conversation()
    send("quiero 3 cuotas desde 2026-10-20")
    for _ in range(2):
        reply = send("el viernes o el 20")
        assert reply["stage"] == "awaiting_terms" and reply["offer_id"] is None
    reply = send("el viernes o el 20")
    assert reply["stage"] == "handoff" and "asesor" in reply["text"]
    assert not ctx.repository.agreements()
    assert ctx.repository.get_session(sid)["pending_offer_id"] is None


def test_unrecognized_date_clarifies_without_creating_agreement(date_conversation, monkeypatch):
    send, ctx, sid = date_conversation()
    send("quiero 3 cuotas desde 2026-10-20")
    model_stub(
        monkeypatch,
        {
            "intent": "unclear",
            "payment_date": "2026-10-31",
            "date_reference": {"quote": "a fin de mes", "day": 31},
        },
        [],
    )
    ctx.provider = "anthropic"
    reply = send("a fin de mes")
    assert "día, mes y año" in reply["text"] and "error del servicio" not in reply["text"]
    assert reply["offer_id"] is None and not ctx.repository.agreements()
    assert ctx.repository.get_session(sid)["draft_terms"]["installments"] == 3
