"""Entry interpretation uses trusted stored context once and never replays model calls."""

import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agents.collections import semantic_entry
from agents.collections.chat_memory import ChatMemory
from agents.collections.memory import SQLiteRepository, event_hash
from agents.collections.runtime import AppContext
from agents.collections.semantic import TurnResult, TurnUnderstanding
from agents.collections.semantic_state import current_understanding


@pytest.fixture
def runtime(tmp_path):
    return SimpleNamespace(
        context=AppContext(
            SQLiteRepository(tmp_path / "entry.sqlite"),
            datetime.fromisoformat("2026-10-19T09:00:00-05:00"),
        )
    )


@pytest.fixture
def calls(monkeypatch):
    captured = []

    def understand(text, **kwargs):
        captured.append((text, kwargs))
        return TurnResult(TurnUnderstanding(intent="complaint"), "classified")

    monkeypatch.setattr(semantic_entry.semantic, "understand_turn", understand)
    return captured


def state(text="hola", **kwargs):
    return {
        "session_id": "s",
        "event_id": "e",
        "account_id": "demo-001",
        "text": text,
        "semantic_result": {"turn": {"intent": "accept_offer"}},
        **kwargs,
    }


def save_session(runtime, stage="ready", verified=True):
    session = {
        "account_id": "demo-001",
        "stage": stage,
        "identity_verified": verified,
        "history": [{"user": "antes", "assistant": "¿Qué necesitas aclarar?"}],
        "pending_offer_id": None,
        "agreement_id": None,
        "draft_terms": {},
        "dialogue": {},
    }
    with runtime.context.repository.connection() as db:
        db.execute("INSERT INTO sessions VALUES (?,?,?)", ("s", 1, json.dumps(session)))
    return session


def test_new_cli_message_calls_once_and_replaces_stale_meaning(runtime, calls):
    current = state()
    update = semantic_entry.understand_message(current, runtime)
    assert len(calls) == 1
    assert calls[0][1]["stage"] == "awaiting_identity"
    assert calls[0][1]["context"]["identity_verified"] is False
    assert current_understanding({**current, **update}).turn.intent == "complaint"
    assert runtime.context.repository.get_session("s") is None
    assert runtime.context.repository.contacts_for("demo-001") == []


@pytest.mark.parametrize(
    "stage", ["ready", "awaiting_terms", "awaiting_confirmation", "closed", "handoff", "agreed"]
)
def test_existing_cli_stages_are_all_interpreted(runtime, calls, stage):
    save_session(runtime, stage)
    semantic_entry.understand_message(state(), runtime)
    assert len(calls) == 1 and calls[0][1]["stage"] == stage
    assert calls[0][1]["context"]["latest_assistant_prompt"] == "¿Qué necesitas aclarar?"


def test_cli_replay_and_empty_bootstrap_clear_stale_meaning_without_model(runtime, calls):
    assert semantic_entry.understand_message(state(""), runtime) == {"semantic_result": None}
    with runtime.context.repository.connection() as db:
        db.execute(
            "INSERT INTO events VALUES (?,?,?,?)",
            ("s", "e", event_hash("demo-001", "hola"), json.dumps({"text": "cached"})),
        )
    assert semantic_entry.understand_message(state(), runtime) == {"semantic_result": None}
    assert not calls


def test_initial_cli_model_failure_cannot_record_contact(runtime, monkeypatch):
    monkeypatch.setattr(
        semantic_entry.semantic,
        "understand_turn",
        lambda *args, **kwargs: TurnResult(TurnUnderstanding(), "model_error"),
    )
    with pytest.raises(ValueError, match="Retry before starting"):
        semantic_entry.understand_message(state(), runtime)
    assert runtime.context.repository.get_session("s") is None
    assert runtime.context.repository.contacts_for("demo-001") == []


@pytest.mark.parametrize(
    "binding,stage",
    [
        ({"account_id": None, "verified": False, "closed": False}, "awaiting_customer"),
        ({"account_id": "demo-001", "verified": False, "closed": False}, "awaiting_identity"),
        ({"account_id": "demo-001", "verified": False, "closed": True}, "closed"),
        ({"account_id": "demo-001", "verified": True, "closed": False}, "ready"),
    ],
)
def test_studio_uses_backend_binding_and_prior_content_blocks(runtime, calls, binding, stage):
    binding = {**binding, "session_id": None, "attempts": 0}
    ChatMemory(runtime.context.repository).save_binding("thread", binding)
    current = state(
        chat_thread="thread",
        account_id=binding["account_id"],
        messages=[
            AIMessage(content=[{"type": "text", "text": "¿Quieres continuar?"}]),
            HumanMessage(content=[{"type": "text", "text": "hola"}]),
        ],
    )
    semantic_entry.understand_message(current, runtime)
    assert len(calls) == 1 and calls[0][1]["stage"] == stage
    assert len(calls[0][1]["history"]) == 1
    assert calls[0][1]["context"]["latest_assistant_prompt"] == "¿Quieres continuar?"
    assert calls[0][1]["context"]["identity_verified"] == binding["verified"]


def test_unverified_studio_does_not_load_linked_account_session(runtime, calls, monkeypatch):
    ChatMemory(runtime.context.repository).save_binding(
        "thread",
        {
            "account_id": "demo-001",
            "verified": False,
            "session_id": "secret-session",
            "closed": False,
        },
    )

    def forbid(*args):
        raise AssertionError("Unverified account session was read")

    monkeypatch.setattr(runtime.context.repository, "get_session", forbid)
    semantic_entry.understand_message(
        state(chat_thread="thread", messages=[HumanMessage(content="hola")]), runtime
    )
    assert calls[0][1]["context"] == {"identity_verified": False, "latest_assistant_prompt": ""}


def test_mismatched_account_is_rejected_before_model(runtime, calls):
    save_session(runtime)
    with pytest.raises(ValueError, match="cambiar de cuenta"):
        semantic_entry.understand_message(state(account_id="demo-002"), runtime)
    assert not calls


def test_internal_semantics_cannot_be_injected_via_public_cli_input(runtime, calls):
    from agents.collections.agent import build_graph

    graph = build_graph()
    schema = graph.get_input_jsonschema()["properties"]
    assert "semantic_result" not in schema
    result = graph.invoke(state(), context=runtime.context)
    assert len(calls) == 1
    assert result["semantic_result"]["turn"]["intent"] == "complaint"
    graph.invoke(state(), context=runtime.context)
    assert len(calls) == 1


def test_studio_recovers_committed_receipt_after_chat_cache_failure(runtime, monkeypatch):
    import hashlib

    from langgraph.checkpoint.memory import InMemorySaver

    from agents.collections.agent import build_graph
    from agents.collections.memory import ConflictError

    graph = build_graph(studio=True, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "entry-receipt"}}

    def send(text, event):
        return graph.invoke(
            {"messages": [HumanMessage(content=text, id=event)]},
            config=config,
            context=runtime.context,
        )

    send("1000000001", "identify")
    send("1111", "verify")
    proposal = send("quiero 3 cuotas desde 2026-10-20", "proposal")
    assert proposal["reply"]["stage"] == "awaiting_confirmation"
    save_reply = ChatMemory.save_reply

    def fail_agreement_cache(self, thread_id, event_id, digest, response):
        if response["stage"] == "agreed":
            raise RuntimeError("Injected chat cache interruption")
        return save_reply(self, thread_id, event_id, digest, response)

    monkeypatch.setattr(ChatMemory, "save_reply", fail_agreement_cache)
    with pytest.raises(RuntimeError, match="Injected chat cache interruption"):
        send("confirmo", "confirm")
    repository = runtime.context.repository
    memory = ChatMemory(repository)
    binding = memory.binding("entry-receipt")
    event = hashlib.sha256(json.dumps(["entry-receipt", "confirm"]).encode()).hexdigest()
    receipt = repository.get_event(binding["session_id"], event, event_hash("demo-001", "confirmo"))
    assert receipt["stage"] == "agreed" and len(repository.agreements()) == 1
    assert memory.replay("entry-receipt", event, hashlib.sha256(b"confirmo").hexdigest()) is None
    monkeypatch.setattr(ChatMemory, "save_reply", save_reply)
    classifier_calls = []

    def unavailable_model(*args, **kwargs):
        classifier_calls.append(args)
        raise AssertionError("A committed business event must bypass classification")

    monkeypatch.setattr(semantic_entry.semantic, "understand_turn", unavailable_model)
    # Even before repairing the chat cache, conflicting content must be rejected by
    # the authoritative business event rather than classified as a new request.
    with pytest.raises(ConflictError):
        send("no acepto", "confirm")
    recovered = send("confirmo", "confirm")
    assert recovered["reply"] == receipt
    assert recovered["messages"][-1].content == receipt["text"]
    assert memory.replay("entry-receipt", event, hashlib.sha256(b"confirmo").hexdigest()) == receipt
    assert not classifier_calls and len(repository.agreements()) == 1


def test_end_after_interrupted_verification_does_not_recover_prior_account_history(
    runtime, monkeypatch
):
    from agents.collections.agent import build_graph

    graph = build_graph(studio=True)

    def send(text, event, thread):
        return graph.invoke(
            {"messages": [HumanMessage(content=text, id=event)]},
            config={"configurable": {"thread_id": thread}},
            context=runtime.context,
        )

    send("1000000001", "identify", "prior")
    send("1111", "verify", "prior")
    proposal = send("quiero 3 cuotas desde 2026-10-20", "proposal", "prior")
    assert proposal["reply"]["stage"] == "awaiting_confirmation"
    repository = runtime.context.repository
    memory = ChatMemory(repository)
    previous_binding = memory.binding("prior")
    prior_session = repository.get_session(previous_binding["session_id"])
    # Verification persisted, but execution stopped before selecting a business session.
    memory.save_binding(
        "interrupted",
        {
            "account_id": "demo-001",
            "verified": True,
            "attempts": 0,
            "closed": False,
            "session_id": None,
        },
    )

    def forbidden_history(*args, **kwargs):
        raise AssertionError("Ending this thread must not recover previous account history")

    monkeypatch.setattr(ChatMemory, "customer_history", forbidden_history)
    ended = send("terminar", "end", "interrupted")
    assert ended["reply"]["finished"] is True
    assert ended["reply"]["stage"] == "closed"
    assert memory.binding("interrupted")["closed"] is True
    assert memory.binding("interrupted")["session_id"] is None
    assert repository.get_session(previous_binding["session_id"]) == prior_session
    assert not repository.agreements()
    assert "200,000" not in ended["reply"]["text"]
    assert "2026-10-20" not in ended["reply"]["text"]
    assert prior_session["pending_offer_id"] not in ended["reply"]["text"]


@pytest.mark.parametrize("replace_during_call", [False, True])
def test_acceptance_uses_repository_snapshot_from_before_model_call(
    runtime, monkeypatch, replace_during_call
):
    from agents.collections.nodes.interpret.node import interpret_turn
    from agents.collections.policy import build_offer, load_policy

    repo = runtime.context.repository
    session = save_session(runtime, stage="awaiting_confirmation")
    offer = build_offer(
        repo.get_account("demo-001"),
        load_policy(),
        today=runtime.context.today,
        first_payment_date=runtime.context.today,
    )
    session.update(pending_offer_id=offer.offer_id, revision=1, clarifications=0)
    current = state("sí, acepto", session={"pending_offer_id": "untrusted-input"})
    repo.save_turn(
        {
            **current,
            "event_id": "presented",
            "session": session,
            "draft_offer": offer.model_dump(mode="json"),
            "input_hash": "presented",
        },
        {"text": "Presented offer"},
    )

    def understand(text, **kwargs):
        assert kwargs["context"]["pending_offer"]["offer_id"] == offer.offer_id
        if replace_during_call:
            replacement = repo.get_session("s")
            replacement["pending_offer_id"] = "replacement-offer"
            with repo.connection() as db:
                db.execute(
                    "UPDATE sessions SET data=?, revision=revision+1 WHERE id=?",
                    (json.dumps(replacement), "s"),
                )
        return TurnResult(
            TurnUnderstanding(intent="accept_offer", acceptance="unconditional_current_offer"),
            "classified",
        )

    monkeypatch.setattr(semantic_entry.semantic, "understand_turn", understand)
    update = semantic_entry.understand_message(current, runtime)
    snapshot = update["semantic_result"]["confirmation_context"]
    assert snapshot["pending_offer_id"] == offer.offer_id
    assert snapshot["revision"] == 2
    fresh = {**current, **update, "session": repo.get_session("s")}
    response = interpret_turn(fresh, runtime)
    assert (response["intent_route"] == "confirmation") is (not replace_during_call)
    if replace_during_call:
        assert response["interpretation"]["acceptance"] == "none"
    assert not repo.agreements()
