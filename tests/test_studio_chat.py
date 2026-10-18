"""Exercise human-only input, identity isolation, recall, and idempotent persistence."""

import socket
from datetime import datetime

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agents.collections.agent import build_graph
from agents.collections.chat_memory import ChatMemory, summarize_history
from agents.collections.memory import ConflictError, SQLiteRepository
from agents.collections.runtime import AppContext


@pytest.fixture
def chat(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")

    def fail_network(*args, **kwargs):
        raise AssertionError("Chat tests must not use external services")

    monkeypatch.setattr(socket.socket, "connect", fail_network)
    ctx = AppContext(
        SQLiteRepository(tmp_path / "chat.sqlite3"),
        datetime.fromisoformat("2026-10-19T09:00:00-05:00"),
    )
    graph = build_graph(studio=True, checkpointer=InMemorySaver())

    def send(text, event, thread="first", **extra):
        return graph.invoke(
            {"messages": [HumanMessage(content=text, id=event)], **extra},
            config={"configurable": {"thread_id": thread}},
            context=ctx,
        )

    return send, ctx, graph


def identify(send, thread="first", customer="1000000001", code="1111"):
    greeting = send("Hola", "hello", thread)
    assert greeting["reply"]["stage"] == "awaiting_customer"
    found = send("Mi cédula es " + customer, "customer", thread)
    assert found["reply"]["stage"] == "awaiting_identity"
    assert "600,000" not in found["messages"][-1].content
    return send("verificar " + code, "verify", thread)


def agree(send):
    assert identify(send)["reply"]["stage"] == "ready"
    proposal = send("quiero 3 cuotas desde 2026-10-20", "proposal")
    assert proposal["reply"]["stage"] == "awaiting_confirmation"
    return send("confirmo", "confirm")


def test_messages_only_end_to_end_and_replay(chat):
    send, ctx, graph = chat
    final = agree(send)
    assert isinstance(final["messages"][-1], AIMessage)
    assert final["reply"]["stage"] == "agreed"
    assert len(ctx.repository.agreements()) == 1
    replay = send("confirmo", "confirm")
    assert replay["messages"] == final["messages"]
    assert replay["reply"] == final["reply"]
    assert len(ctx.repository.agreements()) == 1
    with pytest.raises(ConflictError):
        send("no acepto", "confirm")
    assert {
        "human_input",
        "identify_customer",
        "verify_identity",
        "recover_history",
        "negotiation",
        "commitment",
    } <= graph.get_graph().nodes.keys()


def test_new_thread_requires_verification_then_recalls_agreement(chat):
    send, ctx, _ = chat
    final = agree(send)
    greeting = send("Qué acordamos", "hello", "returning")
    assert final["reply"]["agreement_id"] not in greeting["messages"][-1].content
    send("1000000001", "customer", "returning")
    wrong = send("verificar 0000", "wrong", "returning")
    assert "600,000" not in wrong["messages"][-1].content
    recalled = send("verificar 1111", "verify", "returning")
    assert final["reply"]["agreement_id"] in recalled["messages"][-1].content
    assert "2026-12-20" in recalled["messages"][-1].content
    assert "pago no verificado" in recalled["messages"][-1].content
    assert len(ctx.repository.agreements()) == 1
    assert len(ctx.repository.contacts_for("demo-001")) == 1


def test_other_customer_never_receives_first_customer_history(chat):
    send, _, _ = chat
    first = agree(send)
    other = identify(send, "other", "1000000002", "2222")
    assert first["reply"]["agreement_id"] not in str(other)
    assert "450,000" in other["messages"][-1].content
    assert "600,000" not in str(other)


def test_new_thread_resumes_pending_offer_without_new_contact(chat):
    send, ctx, _ = chat
    identify(send)
    send("quiero 3 cuotas desde 2026-10-20", "proposal")
    recalled = identify(send, "returning")
    assert "Propuesta pendiente" in recalled["messages"][-1].content
    assert "2026-12-20" in recalled["messages"][-1].content
    assert send("confirmo", "confirm", "returning")["reply"]["stage"] == "agreed"
    assert len(ctx.repository.contacts_for("demo-001")) == 1


def test_public_fields_cannot_inject_backend_identity(chat):
    send, ctx, _ = chat
    result = send(
        "quiero pagar",
        "fake",
        account_id="demo-001",
        session_id="forged",
        chat_route="intake",
        session={"identity_verified": True},
    )
    assert result["reply"]["stage"] == "awaiting_customer"
    assert ctx.repository.get_session("forged") is None
    assert not ctx.repository.agreements()


def test_three_wrong_codes_close_only_that_thread(chat):
    send, ctx, _ = chat
    send("1000000001", "customer")
    for i in range(3):
        result = send("verificar 0000", str(i))
        assert "600,000" not in result["messages"][-1].content
    assert result["reply"]["stage"] == "closed"
    assert send("verificar 1111", "late")["reply"]["stage"] == "closed"
    assert ctx.repository.contacts_for("demo-001") == []


def test_replayed_wrong_code_does_not_consume_attempts(chat):
    send, ctx, _ = chat
    send("1000000001", "customer")
    send("verificar 0000", "wrong")
    send("verificar 0000", "wrong")
    assert ChatMemory(ctx.repository).binding("first")["attempts"] == 1


def test_backend_recovers_even_without_checkpoint_history(chat):
    send, ctx, _ = chat
    agree(send)
    restarted = build_graph(studio=True)
    result = restarted.invoke(
        {"messages": [HumanMessage(content="Qué acordamos", id="recall")]},
        config={"configurable": {"thread_id": "first"}},
        context=ctx,
    )
    assert "Compromiso" in result["messages"][-1].content


def test_summary_is_bounded_and_preserves_source_records(chat):
    _, ctx, _ = chat
    with ctx.repository.connection() as db:
        import json

        for i in range(20):
            record = {
                "account_id": "demo-001",
                "identity_verified": True,
                "stage": "closed",
                "history": [{"user": "x" * 10000, "assistant": "y" * 10000}] * 8,
            }
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (f"old-{i}", 1, json.dumps(record)))
    memory = ChatMemory(ctx.repository)
    history = memory.customer_history("demo-001")
    assert history["count"] == 20
    assert len(history["sessions"]) == 3
    assert len(summarize_history(history)) < 3000
    assert len(ctx.repository.get_session("old-0")["history"][0]["user"]) == 10000


@pytest.mark.parametrize("selected", [False, True])
def test_stop_during_onboarding_never_reads_history(chat, monkeypatch, selected):
    send, ctx, _ = chat
    if selected:
        send("1000000001", "customer")

    def no_history(*args):
        raise AssertionError("No history access before verification")

    monkeypatch.setattr(ChatMemory, "customer_history", no_history)
    assert send("terminar", "stop")["reply"]["stage"] == "closed"
    assert send("verificar 1111", "late")["reply"]["stage"] == "closed"
    assert ctx.repository.contacts_for("demo-001") == []


def test_failed_assistant_node_does_not_repeat_identity_transition(chat, monkeypatch):
    send, ctx, _ = chat
    send("1000000001", "customer")
    original = ChatMemory.save_reply
    calls = 0

    def fail_delivery(self, *args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated failure after transition commit")
        return original(self, *args)

    monkeypatch.setattr(ChatMemory, "save_reply", fail_delivery)
    with pytest.raises(RuntimeError):
        send("verificar 0000", "wrong")
    assert ChatMemory(ctx.repository).binding("first")["attempts"] == 1
    assert send("verificar 0000", "wrong")["reply"]["stage"] == "awaiting_identity"
    assert ChatMemory(ctx.repository).binding("first")["attempts"] == 1


def test_verification_recovers_after_interrupted_history_node(chat, monkeypatch):
    send, _, _ = chat
    send("1000000001", "customer")
    original = ChatMemory.customer_history

    def fail_history(*args):
        raise RuntimeError("simulated history outage")

    monkeypatch.setattr(ChatMemory, "customer_history", fail_history)
    with pytest.raises(RuntimeError):
        send("verificar 1111", "verify")
    monkeypatch.setattr(ChatMemory, "customer_history", original)
    assert send("verificar 1111", "verify")["reply"]["stage"] == "ready"


def test_studio_text_content_blocks_are_supported(chat):
    _, ctx, graph = chat
    config = {"configurable": {"thread_id": "blocks"}}
    for event, text in enumerate(["Hola", "1000000001", "verificar 1111"]):
        result = graph.invoke(
            {
                "messages": [
                    {"type": "human", "id": str(event), "content": [{"type": "text", "text": text}]}
                ]
            },
            config=config,
            context=ctx,
        )
    assert result["reply"]["stage"] == "ready"
    assert "600,000" in result["messages"][-1].content


def test_attachments_are_rejected_without_identity_side_effects(chat):
    _, ctx, graph = chat
    with pytest.raises(ValueError, match="text only"):
        graph.invoke(
            {
                "messages": [
                    HumanMessage(
                        content=[
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://example.invalid/image.png"},
                            }
                        ]
                    )
                ]
            },
            config={"configurable": {"thread_id": "image"}},
            context=ctx,
        )
    assert not ctx.repository.agreements()


def assert_no_internal_history(text):
    for fragment in (
        "Recuperé",
        "Resumen de las",
        "Estado:",
        "Cliente:",
        "Agente:",
        "conservando el historial",
        "cerró esta sesión por error",
        "PRIVATE_TRANSCRIPT",
    ):
        assert fragment not in text


@pytest.mark.parametrize("outcome", ["ready", "pending", "agreed", "closed", "handoff", "legacy"])
def test_verified_recall_keeps_transcripts_internal_and_preserves_outcomes(chat, outcome):
    import json

    from agents.collections.conversation import LEGACY_CLOSE

    send, ctx, _ = chat
    initial = identify(send)
    assert_no_internal_history(initial["messages"][-1].content)
    assert "conversaciones anteriores" not in initial["messages"][-1].content
    if outcome in {"pending", "agreed"}:
        send("quiero 3 cuotas desde 2026-10-20", "proposal")
    if outcome == "agreed":
        send("confirmo", "confirm")
    elif outcome == "closed":
        send("terminar", "stop")
    elif outcome == "handoff":
        send("quiero un asesor", "advisor")
    session_id = ChatMemory(ctx.repository).binding("first")["session_id"]
    session = ctx.repository.get_session(session_id)
    if outcome == "legacy":
        session.update(stage="closed", pending_offer_id=None)
        session["history"].append(
            {
                "event_id": "legacy",
                "user": "ayudame a programar en python",
                "assistant": LEGACY_CLOSE,
            }
        )
    # The same marker remains in the bounded internal summary, never the chat response.
    session["history"].extend(
        [
            {
                "event_id": f"private-{i}",
                "user": "PRIVATE_TRANSCRIPT",
                "assistant": "PRIVATE_TRANSCRIPT",
            }
            for i in range(3)
        ]
    )
    with ctx.repository.connection() as db:
        db.execute("UPDATE sessions SET data=? WHERE id=?", (json.dumps(session), session_id))
    agreements_before = len(ctx.repository.agreements())
    recalled = identify(send, "returning")
    assert "PRIVATE_TRANSCRIPT" in recalled["history_summary"]
    assert_no_internal_history(recalled["messages"][-1].content)
    assert recalled["reply"]["stage"] == session["stage"]
    assert ctx.repository.get_session(session_id) == session
    assert len(ctx.repository.contacts_for("demo-001")) == 1
    assert len(ctx.repository.agreements()) == agreements_before
    repeated = send("verificar 1111", "verify", "returning")
    assert repeated["reply"] == recalled["reply"]
    requested = send("¿Qué acordamos?", "history-request", "returning")
    assert_no_internal_history(requested["messages"][-1].content)
    if outcome == "legacy":
        assert "opciones de pago" in recalled["reply"]["text"]
        complaint = send("que mal servicio!!!", "complaint", "returning")
        assert_no_internal_history(complaint["reply"]["text"])
        assert complaint["reply"]["stage"] == "closed"
        resumed = send("continuemos", "resume", "returning")
        assert resumed["reply"]["stage"] == "ready"
        assert_no_internal_history(resumed["reply"]["text"])
        assert len(ctx.repository.agreements()) == agreements_before
    elif outcome in {"closed", "handoff", "agreed"}:
        assert send("continuemos", "resume", "returning")["reply"]["stage"] == session["stage"]


@pytest.mark.parametrize("latest_stage", ["ready", "closed", "handoff"])
def test_recall_preserves_commitments_from_earlier_sessions(chat, latest_stage):
    import json

    send, ctx, _ = chat
    agreed = agree(send)
    agreement_id = agreed["reply"]["agreement_id"]
    earlier = ctx.repository.get_session(ChatMemory(ctx.repository).binding("first")["session_id"])
    newer = {
        **earlier,
        "revision": 1,
        "stage": latest_stage,
        "agreement_id": None,
        "pending_offer_id": None,
        "history": [],
        "close_reason": "customer_request",
    }
    with ctx.repository.connection() as db:
        db.execute("INSERT INTO sessions VALUES (?,?,?)", ("newer", 1, json.dumps(newer)))
    verified = identify(send, "returning")
    requested = send("¿Qué acordamos?", "history-request", "returning")
    for result in (verified, requested):
        text = result["reply"]["text"]
        assert agreement_id in text and "2026-12-20" in text
        assert "pago no verificado" in text
        assert "pago total o un plan" not in text
        assert_no_internal_history(text)
        assert result["reply"]["stage"] == latest_stage
    assert ctx.repository.get_session("newer") == newer
    assert len(ctx.repository.agreements()) == 1
    assert len(ctx.repository.contacts_for("demo-001")) == 1
