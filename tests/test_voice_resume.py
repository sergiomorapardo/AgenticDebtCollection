"""Resume one business session across voice calls offline: identity, isolation, idempotency."""

import json
import socket
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agents.collections.memory import SQLiteRepository
from collections_lab.channels.voice_app import create_app


class FakeSpeech:
    def __init__(self):
        self.calls = []

    def synthesize(self, text):
        self.calls.append(text)
        return b"ID3-demo"

    def transcribe(self, path):
        raise AssertionError("These tests use typed turns only")


@pytest.fixture
def voice(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")

    def no_network(*args, **kwargs):
        raise AssertionError("Voice tests must not call paid services")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    speech = FakeSpeech()
    data_dir = tmp_path / "voice"
    app = create_app(
        speech, data_dir=data_dir, now=datetime.fromisoformat("2026-10-19T09:00:00-05:00")
    )
    clients = []

    def browser():
        client = TestClient(app, base_url="http://127.0.0.1")
        client.__enter__()
        clients.append(client)
        assert client.get("/").status_code == 200
        return client

    yield browser, SQLiteRepository(data_dir / "business.sqlite3"), speech, data_dir
    for client in clients:
        client.__exit__(None, None, None)


def post(client, action, *, state=None, payload=None, key=None):
    headers = {
        "X-Voice-Client": "1",
        "X-Request-ID": key or uuid4().hex,
        "X-Call-ID": state["session_id"] if state else "",
    }
    return client.post("/api/" + action, json=payload or {}, headers=headers)


def play(client, state):
    if state["audio_id"] and not state["ready"] and not state["finished"]:
        response = post(client, "playback", state=state, payload={"audio_id": state["audio_id"]})
        assert response.status_code == 200, response.text
        state = response.json()
    return state


def say(client, state, text, *, key=None):
    response = post(client, "text", state=play(client, state), payload={"text": text}, key=key)
    assert response.status_code == 200, response.text
    return response.json()


def call(client, customer="1000000001", code="1111"):
    state = post(client, "start").json()
    state = say(client, state, f"Mi cédula es {customer}")
    return say(client, state, f"verificar {code}")


def offer(client):
    state = call(client)
    state = say(client, state, "quiero 3 cuotas desde 2026-10-20")
    assert state["details"]["stage"] == "awaiting_confirmation"
    return state


def business_sessions(repository):
    with repository.connection() as db:
        rows = db.execute("SELECT id,revision,data FROM sessions").fetchall()
    return {row["id"]: (row["revision"], json.loads(row["data"])) for row in rows}


def transcript(state):
    return " ".join(turn["content"] for turn in state["history"])


def test_verified_customer_explicitly_resumes_a_stopped_session_in_a_new_call(voice):
    browser, repository, _, data_dir = voice
    client = browser()
    first = call(client)
    assert first["details"]["stage"] == "ready"
    stopped = say(client, first, "terminar")
    assert stopped["finished"]
    [business_id] = business_sessions(repository)

    second = play(client, call(client))
    assert second["session_id"] != first["session_id"]
    assert second["details"]["stage"] == "closed"
    assert not second["finished"] and second["ready"]
    assert business_sessions(repository)[business_id][1]["stage"] == "closed"

    key = uuid4().hex
    resumed = say(client, second, "quiero revisar opciones de pago", key=key)
    assert resumed["details"]["stage"] == "awaiting_terms"
    assert say(client, second, "quiero revisar opciones de pago", key=key) == resumed
    proposal = say(client, resumed, "quiero 3 cuotas desde 2026-10-20")
    agreed = say(client, proposal, "confirmo")
    assert agreed["finished"] and agreed["details"]["agreement_id"]
    [agreement] = repository.agreements()
    assert agreement["session_id"] == business_id and not agreement["paid"]
    assert list(business_sessions(repository)) == [business_id]
    assert len(repository.contacts_for("demo-001")) == 1
    assert not list(data_dir.glob("*/conversation.sqlite3"))


def test_new_call_requires_fresh_verification_before_any_history(voice):
    browser, repository, _, _ = voice
    client = browser()
    previous = offer(client)
    assert post(client, "end", state=previous).json()["finished"]
    before = business_sessions(repository)

    state = post(client, "start").json()
    stale = post(client, "text", state=previous, payload={"text": "confirmo"})
    assert stale.status_code == 400 and "cambió" in stale.json()["detail"]
    state = say(client, state, "¿Qué acordamos?")
    state = say(client, state, "Mi cédula es 1000000001")
    for _ in range(3):
        state = say(client, state, "verificar 9999")
    assert state["finished"] and state["details"]["stage"] == "closed"
    for fragment in ("200,000", "600,000", "Propuesta"):
        assert fragment not in transcript(state)
    assert business_sessions(repository) == before
    assert not repository.agreements()


def test_customers_remain_isolated_across_calls(voice):
    browser, repository, _, _ = voice
    client = browser()
    first = say(client, offer(client), "confirmo")
    agreement_id = first["details"]["agreement_id"]

    other = call(browser(), customer="1000000002", code="2222")
    assert other["details"]["stage"] == "ready" and "450,000" in transcript(other)
    assert agreement_id not in transcript(other) and "600,000" not in transcript(other)
    assert len(repository.agreements()) == 1
    assert len(business_sessions(repository)) == 2


@pytest.mark.parametrize("outcome", ["agreed", "handoff"])
def test_ineligible_outcomes_end_the_new_call_without_reopening(voice, outcome):
    browser, repository, _, _ = voice
    client = browser()
    if outcome == "agreed":
        say(client, offer(client), "confirmo")
    else:
        assert say(client, call(client), "quiero un asesor")["finished"]
    before = business_sessions(repository)

    recovered = call(client)
    assert recovered["finished"] and recovered["details"]["stage"] == outcome
    assert business_sessions(repository) == before
    assert len(repository.agreements()) == (outcome == "agreed")
    assert post(client, "start").status_code == 200


def test_offer_from_a_previous_call_must_play_in_the_current_call(voice):
    browser, repository, _, _ = voice
    client = browser()
    previous = play(client, offer(client))
    assert previous["ready"]
    post(client, "end", state=previous)

    recovered = call(client)
    assert recovered["details"]["stage"] == "awaiting_confirmation"
    assert "200,000" in recovered["history"][-1]["content"]
    assert not recovered["ready"]
    blocked = post(client, "text", state=recovered, payload={"text": "confirmo"})
    assert blocked.status_code == 400 and not repository.agreements()
    agreed = say(client, recovered, "confirmo")
    assert agreed["finished"] and len(repository.agreements()) == 1


def test_overlapping_calls_and_retries_record_one_agreement(voice):
    browser, repository, _, _ = voice
    first, second = browser(), browser()
    pending = play(first, offer(first))
    recovered = play(second, call(second))
    assert recovered["details"]["stage"] == "awaiting_confirmation"

    key = uuid4().hex
    agreed = say(first, pending, "confirmo", key=key)
    assert agreed["finished"]
    assert say(first, pending, "confirmo", key=key) == agreed
    late = say(second, recovered, "confirmo")
    assert late["finished"] and late["details"]["stage"] == "agreed"
    assert len(repository.agreements()) == 1


def test_hangup_after_resume_is_local_and_keeps_the_business_outcome(voice, monkeypatch):
    from agents.collections import semantic

    browser, repository, speech, _ = voice
    client = browser()
    say(client, call(client), "terminar")
    recovered = call(client)
    assert not recovered["finished"]
    before, spoken = business_sessions(repository), len(speech.calls)

    def unavailable(*args, **kwargs):
        raise AssertionError("Hangup must not interpret text")

    monkeypatch.setattr(semantic, "understand_turn", unavailable)
    ended = post(client, "end", state=recovered).json()
    assert ended["finished"] and ended["audio_id"] is None
    assert business_sessions(repository) == before
    assert len(speech.calls) == spoken and not repository.agreements()


def test_hot_reloaded_graph_calls_share_the_business_database(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    script = """
import socket
import sys
from datetime import datetime
from pathlib import Path

def no_network(*args, **kwargs):
    raise AssertionError("Offline registry integration must not call providers")
socket.socket.connect = no_network
from agents.collections.memory import SQLiteRepository
from agents.collections.runtime import AppContext
from collections_lab.channels.voice_session import GraphRegistry, VoiceSession

class Speech:
    def synthesize(self, text):
        return b"ID3-offline"

data = Path(sys.argv[1])
registry = GraphRegistry(Path.cwd())
context = AppContext(SQLiteRepository(data / "business.sqlite3"), datetime.fromisoformat("2026-10-19T09:00:00-05:00"))
def voice_call(name):
    call = VoiceSession(context, Speech(), data / name, graph_registry=registry)
    for text in ["Hola", "123456789", "1111"]:
        reply = call.send(text)
    return call, reply
first, _ = voice_call("first")
assert first.send("terminar")["finished"]
first.end_call()
second, recovered = voice_call("second")
assert recovered["stage"] == "closed" and not recovered["finished"]
assert second.send("quiero revisar opciones de pago")["stage"] == "awaiting_terms"
second.end_call()
assert registry._leases == 0 and not context.repository.agreements()
print("registered graph resumes shared business session: OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "shared business session: OK" in result.stdout
