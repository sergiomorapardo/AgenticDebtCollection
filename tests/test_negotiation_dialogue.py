"""Exercise realistic Studio dialogue with persisted state and no external services."""

import socket
from datetime import datetime

import pytest
from langchain_core.messages import HumanMessage

from agents.collections.agent import build_graph
from agents.collections.memory import SQLiteRepository
from agents.collections.runtime import AppContext


@pytest.fixture
def conversation(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")

    def no_network(*args, **kwargs):
        raise AssertionError("Offline dialogue tests cannot call external providers")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    ctx = AppContext(
        SQLiteRepository(tmp_path / "dialogue.sqlite3"),
        datetime.fromisoformat("2026-10-19T09:00:00-05:00"),
    )
    counter = 0

    def send(text, thread="dialogue"):
        nonlocal counter
        counter += 1
        # Rebuild every time: SQLite, not a Python variable, owns business memory.
        graph = build_graph(studio=True)
        return graph.invoke(
            {"messages": [HumanMessage(content=text, id=str(counter))]},
            config={"configurable": {"thread_id": thread}},
            context=ctx,
        )

    send("1000000001")
    send("verificar 1111")
    return send, ctx


def test_screenshot_refusals_are_understood_without_repeating_or_pressuring(conversation):
    send, ctx = conversation
    first = send("no quiero pagar nada")
    assert "dificultad económica" in first["reply"]["text"]
    second = send("no quiero pagar ¿que vas a hacer?")
    assert second["reply"]["stage"] == "closed"
    assert "No insistiré" in second["reply"]["text"]
    assert "AAAA" not in first["reply"]["text"]
    assert not ctx.repository.agreements()


def test_hardship_capacity_counteroffer_and_confirmation(conversation):
    send, ctx = conversation
    assert "gastos esenciales" in send("perdí mi trabajo")["reply"]["text"]
    capacity = send("250 mil")
    assert "200,000" in capacity["reply"]["text"]
    assert capacity["reply"]["offer_id"] is None
    offered = send("mañana")
    assert offered["reply"]["stage"] == "awaiting_confirmation"
    assert not ctx.repository.agreements()
    result = send("confirmo")
    assert result["reply"]["stage"] == "agreed"
    assert [p["amount_cop"] for p in ctx.repository.agreements()[0]["offer"]["installments"]] == [
        200000
    ] * 3


def test_rejected_schedule_is_remembered_after_restart_and_not_reoffered(conversation):
    send, ctx = conversation
    offered = send("quiero 2 cuotas desde mañana")
    send("no me sirve")
    session = ctx.repository.get_session(offered["session_id"])
    assert len(session["rejected_offers"]) == 1
    repeated = send("quiero 2 cuotas desde mañana")
    assert "mismo calendario" in repeated["reply"]["text"]
    assert repeated["reply"]["offer_id"] is None
    assert send("confirmo")["reply"]["agreement_id"] is None
    replacement = send("mejor 3 cuotas desde mañana")
    assert replacement["reply"]["stage"] == "awaiting_confirmation"
    assert send("confirmo")["reply"]["stage"] == "agreed"


@pytest.mark.parametrize("response", ["nada", "cero", "0", "no puedo destinar nada"])
def test_zero_capacity_does_not_trigger_unaffordable_offers(conversation, response):
    send, ctx = conversation
    send("no puedo pagar")
    assert send(response)["reply"]["stage"] == "handoff"
    assert not ctx.repository.agreements()


def test_capacity_below_policy_escalates_without_inventing_discount(conversation):
    send, ctx = conversation
    result = send("solo puedo pagar 100 mil al mes")
    assert result["reply"]["stage"] == "handoff"
    assert not ctx.repository.agreements()
    assert ctx.repository.get_account("demo-001").balance_cop == 600000


def test_amount_frequency_is_clarified_before_proposing_monthly_plan(conversation):
    send, _ = conversation
    first = send("solo puedo pagar 250 mil")
    assert "cada mes" in first["reply"]["text"]
    next_turn = send("cada mes")
    assert "200,000" in next_turn["reply"]["text"]


@pytest.mark.parametrize(
    "text,stage",
    [
        ("no quiero pagar, no quiero continuar", "closed"),
        ("no reconozco esa deuda", "handoff"),
        ("acepto si me descuentan", "handoff"),
    ],
)
def test_stop_dispute_and_discount_never_become_agreements(conversation, text, stage):
    send, ctx = conversation
    send("quiero 3 cuotas desde mañana")
    result = send(text)
    assert result["reply"]["stage"] == stage
    assert result["reply"]["offer_id"] is None
    assert not ctx.repository.agreements()


def test_repeated_objections_have_a_bound(conversation):
    send, ctx = conversation
    for _ in range(3):
        response = send("no me sirve")
    assert response["reply"]["stage"] == "handoff"
    assert not ctx.repository.agreements()


@pytest.mark.parametrize("pending", [False, True])
def test_capacity_survives_installment_changes_and_rejection(conversation, pending):
    send, ctx = conversation
    send("no puedo pagar")
    send("250 mil")
    if pending:
        send("mañana")
    result = send("mejor 2 cuotas desde mañana")
    assert result["reply"]["stage"] == "handoff"
    assert not ctx.repository.agreements()


@pytest.mark.parametrize(
    "stop", ["no quiero pagar, quiero terminar", "no quiero pagar nada, cuelga"]
)
def test_compound_stop_precedes_refusal(conversation, stop):
    send, _ = conversation
    assert send(stop)["reply"]["stage"] == "closed"


def test_one_off_answer_preserves_partial_amount(conversation):
    send, ctx = conversation
    send("solo puedo pagar 250 mil")
    result = send("un único abono")
    assert "no cubre" in result["reply"]["text"]
    assert result["reply"]["offer_id"] is None
    assert ctx.repository.get_session(result["session_id"])["draft_terms"]["amount_cop"] == 250000
    assert send("mañana")["reply"]["offer_id"] is None
