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
