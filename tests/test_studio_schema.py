"""El servidor de Studio expone entradas simples, no dependencias de infraestructura."""

import socket
from datetime import datetime

import pytest
from langgraph.runtime import Runtime

from agents.collections.memory import SQLiteRepository
from agents.collections.runtime import AppContext, resolve_context
from agents.collections.studio import agent


def test_studio_schemas_do_not_expose_repository_or_trusted_state():
    schema = agent.get_input_jsonschema()
    assert set(schema["properties"]) == {"session_id", "event_id", "account_id", "text"}
    assert agent.get_context_jsonschema() is None


@pytest.mark.parametrize(
    "context",
    [
        None,
        {},
        {"thread_id": "transport-thread"},
        {"thread_id": "transport-thread", "thread_ts": "checkpoint"},
    ],
)
def test_studio_context_uses_backend_dependencies(tmp_path, monkeypatch, context):
    database = tmp_path / "studio.sqlite3"
    monkeypatch.setenv("COLLECTIONS_DATABASE_PATH", str(database))
    monkeypatch.setenv("COLLECTIONS_LLM_PROVIDER", "mock")
    monkeypatch.setenv("COLLECTIONS_MODE", "simulation")
    monkeypatch.setenv("COLLECTIONS_DATA_DIR", "")
    monkeypatch.setenv("COLLECTIONS_DEMO_NOW", "2026-10-19T09:00:00-05:00")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")

    def fail_network(*args, **kwargs):
        raise AssertionError("Studio regression tests must not call external services")

    monkeypatch.setattr(socket.socket, "connect", fail_network)
    turns = [
        ("", "awaiting_identity"),
        ("verificar 1111", "ready"),
        ("quiero 3 cuotas desde 2026-10-20", "awaiting_confirmation"),
        ("confirmo", "agreed"),
    ]
    for index, (text, stage) in enumerate(turns):
        result = agent.invoke(
            {
                "session_id": "business-session",
                "account_id": "demo-001",
                "event_id": str(index),
                "text": text,
            },
            context=context,
        )
        assert result["reply"]["stage"] == stage
    repository = SQLiteRepository(database)
    assert len(repository.agreements()) == 1
    assert repository.get_session("business-session")["identity_verified"]
    assert repository.get_session("transport-thread") is None


@pytest.mark.parametrize(
    "context",
    [
        {"provider": "anthropic"},
        {"thread_id": "transport-thread", "identity_verified": True},
        {"repository": "client-controlled"},
        {"thread_id": {"provider": "anthropic"}},
        {"thread_ts": {"provider": "anthropic"}},
        "client-controlled",
        [],
        False,
    ],
)
def test_studio_context_rejects_client_dependencies(context):
    with pytest.raises(ValueError, match="backend local"):
        resolve_context(Runtime(context=context))


def test_backend_can_still_inject_trusted_context(tmp_path):
    context = AppContext(
        SQLiteRepository(tmp_path / "trusted.sqlite3"),
        datetime.fromisoformat("2026-10-19T09:00:00-05:00"),
    )
    assert resolve_context(Runtime(context=context)) is context
