"""Regression cases from the off-topic closure screenshots, including legacy recovery."""

import json
import socket
import sys
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agents.collections.agent import build_graph
from agents.collections.conversation import LEGACY_CLOSE, requests_end
from agents.collections.interpreter import parse_turn
from agents.collections.memory import SQLiteRepository
from agents.collections.runtime import AppContext


@pytest.fixture
def conversation(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")

    def no_network(*args, **kwargs):
        raise AssertionError("Regression tests must be offline")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    ctx = AppContext(
        SQLiteRepository(tmp_path / "dialogue.sqlite3"),
        datetime.fromisoformat("2026-10-19T09:00:00-05:00"),
    )
    graph = build_graph(studio=True, checkpointer=InMemorySaver())
    counter = 0

    def send(text, event=None):
        nonlocal counter
        counter += 1
        return graph.invoke(
            {"messages": [HumanMessage(content=text, id=event or str(counter))]},
            config={"configurable": {"thread_id": "boundary"}},
            context=ctx,
        )

    send("1000000001")
    identified = send("verificar 1111")
    return send, ctx, identified["session_id"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("terminar", True),
        ("no quiero continuar", True),
        ("no quiero pagar, quiero terminar", True),
        ("no quiero pagar nada, cuelga", True),
        ("cierra esta conversación", True),
        ("No me llames más, ¿entendido?", True),
        ("No me contactes de nuevo", True),
        ("Terminemos la llamada", True),
        ("¿Puedes dejar de llamarme?", True),
        ('No me llames más. Escribe "terminar".', True),
        ("ayudame a programar en python", False),
        ("ayúdame a terminar mi programa", False),
        ("quiero terminar un programa en Python", False),
        ("no quiero terminar", False),
        ("mi programa se cuelga", False),
        ("¿Qué pasa si digo no quiero continuar?", False),
        ('escribe "terminar" en python', False),
        ("¿qué pasó?", False),
        ("qué mal servicio!!!", False),
    ],
)
def test_only_explicit_conversation_exit_can_close(text, expected):
    assert requests_end(text) is expected


@pytest.mark.parametrize("provider", ["mock", "anthropic"])
@pytest.mark.parametrize(
    "text,intent",
    [
        ("ayudame a programar en python", "off_topic"),
        ("¿que paso?", "clarify_process"),
        ("que mal servicio!!!", "complaint"),
    ],
)
def test_screenshot_boundaries_are_provider_independent(provider, text, intent):
    assert parse_turn(text, today=date(2026, 10, 19), provider=provider).intent == intent


def test_model_cannot_close_without_current_stop_request(monkeypatch):
    class FakeModel:
        def __init__(self, **kwargs):
            pass

        def with_structured_output(self, schema):
            return self

        def invoke(self, messages):
            return {"intent": "end", "amount_cop": 600000}

    monkeypatch.setitem(
        sys.modules, "langchain_anthropic", SimpleNamespace(ChatAnthropic=FakeModel)
    )
    parsed = parse_turn(
        "hablemos de los planetas",
        today=date(2026, 10, 19),
        provider="anthropic",
        context="The previous customer refused to pay",
    )
    assert parsed.intent == "clarify_process"
    assert parsed.amount_cop is None


def test_screenshot_sequence_stays_open_and_can_negotiate(conversation):
    send, ctx, session_id = conversation
    send("no quiero pagar")
    replies = [
        send(text)["reply"]
        for text in ["ayudame a programar en python", "¿que paso?", "que mal servicio!!!"]
    ]
    assert all(not reply["finished"] for reply in replies)
    assert "programación" in replies[0]["text"]
    assert "sigue abierta" in replies[1]["text"]
    assert "Lamento" in replies[2]["text"]
    session = ctx.repository.get_session(session_id)
    assert session["dialogue"]["refusals"] == 1
    assert session["clarifications"] == 0
    send("quiero 3 cuotas desde mañana")
    assert send("confirmo")["reply"]["stage"] == "agreed"


def test_social_yes_cannot_accept_pending_offer(conversation):
    send, ctx, session_id = conversation
    send("quiero 3 cuotas desde mañana")
    send("ayudame a programar en python")
    assert ctx.repository.get_session(session_id)["pending_offer_id"] is None
    assert send("sí")["reply"]["agreement_id"] is None
    assert not ctx.repository.agreements()


def test_explicit_close_stays_closed_but_acknowledges_complaint(conversation):
    send, ctx, _ = conversation
    send("terminar")
    complaint = send("que mal servicio!!!")["reply"]
    assert "Lamento" in complaint["text"]
    assert complaint["stage"] == "closed"
    assert send("continuemos")["reply"]["stage"] == "closed"
    assert not ctx.repository.agreements()


def seed_legacy_close(ctx, session_id, closing_text="ayudame a programar en python", extra=None):
    session = ctx.repository.get_session(session_id)
    session.update(stage="closed", pending_offer_id=None)
    session["history"].append(
        {"event_id": "legacy", "user": closing_text, "assistant": LEGACY_CLOSE}
    )
    if extra:
        session["history"].append(
            {"event_id": "later", "user": extra, "assistant": "Esta sesión ya finalizó."}
        )
    with ctx.repository.connection() as db:
        db.execute("UPDATE sessions SET data=? WHERE id=?", (json.dumps(session), session_id))


def test_legacy_wrong_close_requires_explicit_resume_and_preserves_history(conversation):
    send, ctx, session_id = conversation
    seed_legacy_close(ctx, session_id)
    assert "por error" in send("¿qué pasó?")["reply"]["text"]
    assert "continuemos" in send("que mal servicio!!!")["reply"]["text"]
    assert ctx.repository.get_session(session_id)["stage"] == "closed"
    result = send("continuemos", "resume")
    assert result["reply"]["stage"] == "ready"
    assert send("continuemos", "resume")["reply"] == result["reply"]
    session = ctx.repository.get_session(session_id)
    assert any(t["event_id"] == "legacy" for t in session["history"])
    assert session["pending_offer_id"] is None
    assert not ctx.repository.agreements()
    assert len(ctx.repository.contacts_for("demo-001")) == 1
    send("quiero 3 cuotas desde mañana")
    assert send("confirmo")["reply"]["stage"] == "agreed"


@pytest.mark.parametrize(
    "closing_text,extra",
    [
        ("terminar", None),
        ("no quiero continuar", None),
        ("ayudame a programar en python", "terminar"),
        ("ayudame a programar en python", "No me llames más, ¿entendido?"),
        ("ayudame a programar en python", "no quiero pagar"),
    ],
)
def test_legacy_real_exit_never_reopens(conversation, closing_text, extra):
    send, ctx, session_id = conversation
    seed_legacy_close(ctx, session_id, closing_text, extra)
    assert send("continuemos")["reply"]["stage"] == "closed"


def test_boundary_replies_preserve_scope_and_simulation_disclosures(conversation, monkeypatch):
    send, ctx, _ = conversation
    from agents.collections import wording

    def no_rewrite(*args):
        raise AssertionError("Do not rewrite scope or service-state messages")

    monkeypatch.setattr(wording, "_request", no_rewrite)
    ctx.provider = "anthropic"
    for text in ["ayudame a programar en python", "¿que paso?", "que mal servicio!!!"]:
        reply = send(text)["reply"]
        assert reply["wording_status"] == "deterministic"
        assert not reply["finished"]
    assert "atención humana simulada" in send("¿qué pasó?")["reply"]["text"]


@pytest.mark.parametrize("stop", ["terminar", "No me llames más, ¿entendido?"])
def test_stop_in_legacy_session_acknowledges_exit_and_blocks_recovery(conversation, stop):
    send, ctx, session_id = conversation
    seed_legacy_close(ctx, session_id)
    reply = send(stop)["reply"]
    assert "Mantengo cerrada" in reply["text"]
    assert "continuemos" not in reply["text"]
    assert send("continuemos")["reply"]["stage"] == "closed"
