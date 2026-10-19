"""Verify the local voice channel offline: transport, persistence, and playback gating."""

import json
import socket
from datetime import datetime

import httpx
import pytest

from agents.collections.memory import SQLiteRepository
from agents.collections.runtime import AppContext
from collections_lab.channels.elevenlabs import ElevenLabsSpeech, SpeechError
from collections_lab.channels.voice_session import VoiceSession


def test_speech_requests_use_expected_host_models_and_secret_header(tmp_path):
    seen = []

    def handle(request):
        seen.append(request)
        assert request.url.host == "api.elevenlabs.io"
        assert request.headers["xi-api-key"] == "test-secret"
        assert "test-secret" not in str(request.url)
        if request.url.path.endswith("speech-to-text"):
            assert b"scribe_v2" in request.content
            assert b"fictional-audio" in request.content
            return httpx.Response(200, json={"text": "  quiero tres cuotas  "})
        assert json.loads(request.content)["model_id"] == "eleven_multilingual_v2"
        return httpx.Response(200, content=b"ID3-audio", headers={"content-type": "audio/mpeg"})

    speech = ElevenLabsSpeech("test-secret", "voice-id", transport=httpx.MockTransport(handle))
    assert speech.synthesize("Hola") == b"ID3-audio"
    audio = tmp_path / "recording.wav"
    audio.write_bytes(b"fictional-audio")
    assert speech.transcribe(audio) == "quiero tres cuotas"
    assert len(seen) == 2


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_provider_errors_are_redacted_and_not_retried(status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json={"detail": "test-secret private provider body"})

    speech = ElevenLabsSpeech("test-secret", "voice-id", transport=httpx.MockTransport(handle))
    with pytest.raises(SpeechError, match=f"HTTP {status}") as error:
        speech.synthesize("Hola")
    assert "test-secret" not in str(error.value)
    assert len(calls) == 1


def test_invalid_input_and_non_audio_success_fail_closed(tmp_path):
    speech = ElevenLabsSpeech(
        "test-secret",
        "voice-id",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
    )
    with pytest.raises(SpeechError):
        speech.synthesize("Hola")
    with pytest.raises(SpeechError):
        speech.synthesize(" ")
    with pytest.raises(SpeechError):
        speech.transcribe(tmp_path / "missing.wav")
    audio = tmp_path / "empty.wav"
    audio.write_bytes(b"")
    with pytest.raises(SpeechError):
        speech.transcribe(audio)
    audio.write_bytes(b"x")
    with pytest.raises(SpeechError, match="inválida"):
        speech.transcribe(audio)
    with pytest.raises(ValueError):
        ElevenLabsSpeech("", "voice-id")
    with pytest.raises(ValueError):
        ElevenLabsSpeech("test-secret", "../../other")


class FakeSpeech:
    def __init__(self):
        self.fail = False
        self.calls = []
        self.transcriptions = []

    def synthesize(self, text):
        self.calls.append(text)
        if self.fail:
            raise SpeechError("Audio unavailable")
        return b"ID3-demo"

    def transcribe(self, path):
        self.transcriptions.append(path)
        return "confirmo"


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")

    def no_network(*args, **kwargs):
        raise AssertionError("Voice tests must not call paid services")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    context = AppContext(
        SQLiteRepository(tmp_path / "conversation.sqlite3"),
        datetime.fromisoformat("2026-10-19T09:00:00-05:00"),
    )
    return VoiceSession(context, FakeSpeech(), tmp_path / "audio")


def offer(session, customer="1000000001"):
    session.send("Hola")
    identified = session.send(f"Mi cédula es {customer}")
    assert identified["stage"] == "awaiting_identity"
    assert "600,000" not in identified["text"]
    session.send("verificar 1111")
    reply = session.send("quiero 3 cuotas desde 2026-10-20")
    assert reply["stage"] == "awaiting_confirmation"
    return reply


@pytest.mark.parametrize("customer", ["1000000001", "123456789"])
def test_confirmation_requires_acknowledgement_for_current_offer(session, customer):
    proposal = offer(session, customer)
    assert session.transcribe("synthetic.wav") == "confirmo"
    assert not session.context.repository.agreements()  # STT never runs the graph.
    with pytest.raises(ValueError, match="Espera a que termine"):
        session.send("confirmo")
    assert not session.context.repository.agreements()
    assert session.acknowledge_playback(session.audio)
    assert session.respond_to_audio("synthetic.wav")["stage"] == "agreed"
    assert len(session.context.repository.agreements()) == 1
    assert session.context.repository.get_account("demo-001").balance_cop == 600000
    evidence = json.loads((session.directory / "playback-acknowledgements.jsonl").read_text())
    assert evidence["offer_id"] == proposal["offer_id"]
    with pytest.raises(ValueError, match="terminó"):
        session.send("confirmo")
    assert len(session.context.repository.agreements()) == 1


def test_audio_failure_preserves_turn_and_retry_does_not_repeat_business_write(session):
    offer(session)
    session.speech.fail = True
    session.acknowledge_playback(session.audio)
    session.send("confirmo")
    assert len(session.context.repository.agreements()) == 1
    assert session.audio is None
    assert "turno ya está guardado" in session.warning
    history = list(session.history)
    session.speech.fail = False
    assert session.retry_audio()
    assert session.history == history
    assert len(session.context.repository.agreements()) == 1


def test_failed_offer_audio_cannot_be_acknowledged_but_can_be_ended(session):
    session.send("Hola")
    session.send("1000000001")
    session.send("1111")
    session.speech.fail = True
    session.send("quiero 3 cuotas desde 2026-10-20")
    with pytest.raises(ValueError, match="Espera a que termine"):
        session.send("confirmo")
    assert session.end_call()["finished"]
    assert not session.context.repository.agreements()


def test_acknowledgement_does_not_carry_over_to_a_changed_offer(session):
    first = offer(session)
    previous_audio = session.audio
    session.acknowledge_playback(previous_audio)
    second = session.send("quiero 2 cuotas desde 2026-10-21")
    assert not session.acknowledge_playback(previous_audio)
    assert first["offer_id"] != second["offer_id"]
    with pytest.raises(ValueError, match="Espera a que termine"):
        session.send("confirmo")
    assert not session.context.repository.agreements()


def test_new_session_has_no_access_to_previous_identity(session, tmp_path):
    offer(session)
    other = VoiceSession(session.context, FakeSpeech(), tmp_path / "other")
    reply = other.send("Qué acordamos")
    assert reply["stage"] == "awaiting_customer"
    assert "600,000" not in reply["text"]


def test_retry_and_stale_playback_do_not_unlock_new_offer_audio(session):
    offer(session)
    previous_audio = session.audio
    assert session.acknowledge_playback(previous_audio)
    session.retry_audio()
    assert not session.acknowledge_playback(previous_audio)
    with pytest.raises(ValueError, match="Espera a que termine"):
        session.respond_to_audio("blocked.wav")
    assert not session.speech.transcriptions
    assert not session.context.repository.agreements()
    assert session.acknowledge_playback(session.audio)
    session.respond_to_audio("confirm.wav")
    assert len(session.context.repository.agreements()) == 1


def test_transcription_failure_never_advances_the_business_turn(session, monkeypatch):
    session.send("Hola")
    history = list(session.history)

    def fail(path):
        raise SpeechError("No se detectó voz")

    monkeypatch.setattr(session.speech, "transcribe", fail)
    with pytest.raises(SpeechError):
        session.respond_to_audio("empty.wav")
    assert session.history == history
    assert len(session.speech.calls) == 1


@pytest.mark.parametrize("text", ["91234567890", "1234567890", "123456789 y 1000000002"])
def test_unknown_or_ambiguous_customer_ids_do_not_select_an_account(session, text):
    reply = session.send(text)
    assert reply["stage"] == "awaiting_customer"
    assert "600,000" not in reply["text"]
    assert not session.context.repository.agreements()


@pytest.fixture
def voice_api(session, tmp_path):
    from fastapi.testclient import TestClient

    from collections_lab.channels.voice_app import create_app

    app = create_app(session.speech, data_dir=tmp_path / "api", now=session.context.now)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]
        assert not session.speech.calls
        yield client


def api_post(client, action, *, state=None, payload=None, key=None, content=None):
    from uuid import uuid4

    headers = {
        "X-Voice-Client": "1",
        "X-Request-ID": key or uuid4().hex,
        "X-Call-ID": state["session_id"] if state else "",
    }
    if content is not None:
        return client.post(
            "/api/" + action, content=content, headers={**headers, "Content-Type": "audio/webm"}
        )
    return client.post("/api/" + action, json=payload or {}, headers=headers)


def finish_playback(client, state):
    response = api_post(client, "playback", state=state, payload={"audio_id": state["audio_id"]})
    assert response.status_code == 200
    return response.json()


def test_voice_api_session_audio_isolation_and_request_replay(voice_api, session):
    from fastapi.testclient import TestClient

    key = "a" * 32
    first = api_post(voice_api, "start", key=key)
    assert first.status_code == 200
    state = first.json()
    assert len(session.speech.calls) == 1
    assert api_post(voice_api, "start", key=key).json() == state
    assert len(session.speech.calls) == 1
    assert voice_api.get(state["audio_url"]).content == b"ID3-demo"
    with TestClient(voice_api.app, base_url="http://127.0.0.1") as other:
        assert other.get(state["audio_url"]).status_code == 401
        other.get("/")
        assert other.get("/api/session").json()["session_id"] is None
        assert other.get(state["audio_url"]).status_code == 404
    assert "test-secret" not in first.text
    assert "/api/audio/" in state["audio_url"]
    assert (
        api_post(voice_api, "text", state=state, key=key, payload={"text": "Hola"}).status_code
        == 409
    )


def test_voice_api_recording_is_one_turn_and_hangup_has_no_speech_cost(voice_api, session):
    state = api_post(voice_api, "start").json()
    assert api_post(voice_api, "recording", state=state, content=b"demo").status_code == 400
    assert not session.speech.transcriptions
    state = finish_playback(voice_api, state)
    key = "b" * 32
    response = api_post(voice_api, "recording", state=state, content=b"demo", key=key)
    assert response.status_code == 200
    assert len(response.json()["history"]) == 4
    assert len(session.speech.transcriptions) == 1
    assert (
        api_post(voice_api, "recording", state=state, content=b"demo", key=key).json()
        == response.json()
    )
    assert len(session.speech.transcriptions) == 1
    assert (
        api_post(voice_api, "recording", state=state, content=b"changed", key=key).status_code
        == 409
    )
    count = len(session.speech.calls)
    closed = api_post(voice_api, "end", state=response.json()).json()
    assert closed["finished"] and closed["audio_id"] is None
    assert len(session.speech.calls) == count
    assert not list(session.directory.parent.rglob("input-*"))


def test_voice_api_nine_digit_id_offer_and_stale_playback(voice_api):
    state = api_post(voice_api, "start").json()
    for text in ["Mi cédula es 123456789", "verificar 1111", "quiero 3 cuotas desde 2026-10-20"]:
        state = finish_playback(voice_api, state)
        response = api_post(voice_api, "text", state=state, payload={"text": text})
        assert response.status_code == 200
        state = response.json()
    assert state["details"]["stage"] == "awaiting_confirmation"
    names = [event["name"] for event in voice_api.get("/api/trace").json()["events"]]
    assert "negotiation" in names and "commitment" not in names
    assert api_post(voice_api, "text", state=state, payload={"text": "confirmo"}).status_code == 400
    old_audio = state["audio_id"]
    state = api_post(voice_api, "retry", state=state).json()
    assert (
        api_post(voice_api, "playback", state=state, payload={"audio_id": old_audio}).status_code
        == 400
    )
    state = finish_playback(voice_api, state)
    result = api_post(voice_api, "text", state=state, payload={"text": "confirmo"}).json()
    assert result["finished"] and result["details"]["agreement_id"]
    names = [event["name"] for event in voice_api.get("/api/trace").json()["events"]]
    assert "confirmation" in names and "commitment" in names
    new_call = api_post(voice_api, "start").json()
    assert new_call["session_id"] != result["session_id"]
    assert api_post(voice_api, "end", state=result).status_code == 400


def test_voice_api_rejects_cross_origin_invalid_and_oversized_requests(voice_api, session):
    from uuid import uuid4

    headers = {"X-Voice-Client": "1", "X-Request-ID": uuid4().hex}
    assert voice_api.post("/api/start", json={}).status_code == 403
    assert (
        voice_api.post(
            "/api/start", json={}, headers={**headers, "Origin": "https://other.example"}
        ).status_code
        == 403
    )
    assert (
        voice_api.post(
            "/api/start", json={}, headers={**headers, "Host": "other.example"}
        ).status_code
        == 400
    )
    assert voice_api.post("/api/start", content="[", headers=headers).status_code == 400
    assert voice_api.post("/api/recording", content=b"x", headers=headers).status_code == 415
    assert (
        voice_api.post(
            "/api/recording",
            content=b"x" * (10 * 1024 * 1024 + 1),
            headers={**headers, "Content-Type": "audio/webm"},
        ).status_code
        == 413
    )
    assert not session.speech.calls


def test_voice_api_clock_survives_reload_and_stops_after_hangup(voice_api, monkeypatch):
    import collections_lab.channels.voice_app as module

    clock = [1000.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    state = api_post(voice_api, "start").json()
    clock[0] += 15
    assert voice_api.get("/api/session").json()["elapsed_seconds"] == 15
    closed = api_post(voice_api, "end", state=state).json()
    clock[0] += 30
    assert voice_api.get("/api/session").json()["elapsed_seconds"] == closed["elapsed_seconds"]


def test_voice_api_caches_uncertain_failure_without_repeating_turn(voice_api, session, monkeypatch):
    state = finish_playback(voice_api, api_post(voice_api, "start").json())
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(True)
        raise RuntimeError("unexpected provider failure")

    monkeypatch.setattr(session.speech, "synthesize", fail)
    key = "c" * 32
    response = api_post(voice_api, "text", state=state, payload={"text": "123456789"}, key=key)
    assert response.status_code == 500
    assert "unexpected provider" not in response.text
    replay = api_post(voice_api, "text", state=state, payload={"text": "123456789"}, key=key)
    assert replay.json() == response.json()
    assert len(attempts) == 1
    assert len(voice_api.get("/api/session").json()["history"]) == 4


def test_execution_trace_matches_graph_and_excludes_customer_content(session):
    session.send("Mi cédula es 123456789")
    trace = session.trace.snapshot()
    assert trace["turn"] == 1
    assert set(trace["topology"]["nodes"]) == set(session.graph.get_graph().nodes)
    assert [event["name"] for event in trace["events"]] == [
        "human_input",
        "understand_message",
        "route_chat_message",
        "identify_customer",
        "assistant_message",
        "text_to_speech",
    ]
    assert all(event["status"] == "complete" for event in trace["events"])
    assert all(event["duration_ms"] >= 0 for event in trace["events"])
    serialized = json.dumps(trace)
    for private in ("123456789", "Mi cédula", "1111", "test-secret", "demo-001"):
        assert private not in serialized
    assert "started" not in serialized
    session.send("1111")
    assert session.trace.snapshot()["turn"] == 2
    assert "verify_identity" in [event["name"] for event in session.trace.snapshot()["events"]]


def test_trace_audio_retry_and_failures_do_not_replay_nodes(session, monkeypatch):
    session.send("Hola")
    original = session.trace.snapshot()["events"]
    session.speech.fail = True
    session.retry_audio()
    failed = session.trace.snapshot()["events"]
    assert failed[:-1] == original
    assert failed[-1]["name"] == "text_to_speech"
    assert failed[-1]["status"] == "error"

    def fail(path):
        raise SpeechError("private-transcript-secret")

    monkeypatch.setattr(session.speech, "transcribe", fail)
    with pytest.raises(SpeechError):
        session.respond_to_audio("test.wav")
    events = session.trace.snapshot()["events"]
    assert len(events) == 1 and events[0]["status"] == "error"
    assert events[0]["name"] == "speech_to_text"
    assert "private-transcript-secret" not in json.dumps(events)


def test_trace_is_readable_during_a_running_node_and_scoped_to_browser(voice_api, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from fastapi.testclient import TestClient
    from langgraph.graph import END, START, StateGraph

    import collections_lab.channels.voice_session as module

    entered, release = Event(), Event()

    def blocked_node(state):
        entered.set()
        assert release.wait(5)
        return {"reply": {"text": "Synthetic response", "finished": False, "stage": "ready"}}

    builder = StateGraph(dict)
    builder.add_node("blocked_node", blocked_node)
    builder.add_edge(START, "blocked_node")
    builder.add_edge("blocked_node", END)
    graph = builder.compile()
    monkeypatch.setattr(module, "build_graph", lambda **kwargs: graph)
    initial = voice_api.get("/api/trace").json()
    assert not initial["events"] and initial["session_id"] is None
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(api_post, voice_api, "start", key="d" * 32)
        try:
            assert entered.wait(3)
            snapshot = pool.submit(voice_api.get, "/api/trace").result(timeout=2).json()
            assert snapshot["events"][0]["name"] == "blocked_node"
            assert snapshot["events"][0]["status"] == "running"
            assert not pending.done()
            with TestClient(voice_api.app, base_url="http://127.0.0.1") as other:
                assert other.get("/api/trace").status_code == 401
                other.get("/")
                assert other.get("/api/trace").json()["events"] == []
        finally:
            release.set()
        result = pending.result(timeout=3)
    assert result.status_code == 200
    trace = voice_api.get("/api/trace").json()
    assert trace["events"][0]["status"] == "complete"
    assert trace["session_id"] == result.json()["session_id"]
    api_post(voice_api, "start", key="d" * 32)
    assert voice_api.get("/api/trace").json() == trace


def test_trace_reports_graph_failure_without_exception_text(voice_api, monkeypatch):
    from langgraph.graph import END, START, StateGraph

    import collections_lab.channels.voice_session as module

    def failed_node(state):
        raise RuntimeError("private-provider-secret")

    builder = StateGraph(dict)
    builder.add_node("failed_node", failed_node)
    builder.add_edge(START, "failed_node")
    builder.add_edge("failed_node", END)
    monkeypatch.setattr(module, "build_graph", lambda **kwargs: builder.compile())
    assert api_post(voice_api, "start").status_code == 500
    trace = voice_api.get("/api/trace").json()
    assert trace["events"][0]["status"] == "error"
    assert "private-provider-secret" not in json.dumps(trace)


def registry_fixture(tmp_path, monkeypatch):
    from uuid import uuid4

    from collections_lab.channels.voice_session import GraphRegistry

    root = tmp_path / "project"
    source = root / "src"
    source.mkdir(parents=True)
    name = "graph_fixture_" + uuid4().hex
    module = source / f"{name}.py"
    template = """from langgraph.graph import StateGraph, START, END
builder = StateGraph(dict)
builder.add_node("{node}", lambda state: {{"marker": "{node}"}})
builder.add_edge(START, "{node}")
builder.add_edge("{node}", END)
agent = builder.compile()
"""
    module.write_text(template.format(node="alpha"))
    manifest = root / "langgraph.json"
    manifest.write_text(json.dumps({"graphs": {"collections_agent": f"./src/{name}.py:agent"}}))
    monkeypatch.syspath_prepend(str(source))
    return GraphRegistry(root), module, manifest, template


def test_graph_registry_refreshes_between_calls_and_discovers_registered_graphs(
    tmp_path, monkeypatch
):
    registry, module, manifest, template = registry_fixture(tmp_path, monkeypatch)
    first = registry.catalog()
    graph, graph_id, version = registry.acquire()
    assert graph_id == "collections_agent"
    assert graph.invoke({}) == {"marker": "alpha"}
    module.write_text(template.format(node="bravo"))  # Same size and second: avoid stale pyc.
    pending = registry.catalog()
    assert pending["pending_update"] and pending["version"] == version
    assert graph.invoke({}) == {"marker": "alpha"}
    registry.release()
    changed = registry.catalog()
    assert changed["version"] != first["version"]
    assert "bravo" in changed["graphs"][0]["topology"]["nodes"]
    content = json.loads(manifest.read_text())
    content["graphs"]["second_flow"] = content["graphs"]["collections_agent"]
    manifest.write_text(json.dumps(content))
    assert {item["id"] for item in registry.catalog()["graphs"]} == {
        "collections_agent",
        "second_flow",
    }


def test_graph_registry_retains_last_valid_version_on_invalid_edit(tmp_path, monkeypatch):
    registry, module, _manifest, template = registry_fixture(tmp_path, monkeypatch)
    first = registry.catalog()
    module.write_text("private-secret invalid Python!")
    failed = registry.catalog()
    assert failed["error"] and failed["pending_update"]
    assert failed["version"] == first["version"]
    assert "private-secret" not in json.dumps(failed)
    graph, _, _ = registry.acquire()
    assert graph.invoke({}) == {"marker": "alpha"}
    registry.release()
    module.write_text(template.format(node="alpha"))
    assert not registry.catalog()["error"]
    assert registry.catalog()["version"] == first["version"]
    module.write_text(template.format(node="delta"))
    assert not registry.catalog()["error"]
    assert "delta" in registry.catalog()["graphs"][0]["topology"]["nodes"]


def test_automatic_layout_and_trace_handle_parallel_nodes_and_renamed_terminal():
    import operator
    from typing import Annotated, TypedDict

    from langgraph.graph import END, START, StateGraph

    from collections_lab.channels.voice_session import ExecutionTrace

    class ParallelState(TypedDict):
        visited: Annotated[list[str], operator.add]

    builder = StateGraph(ParallelState)
    for name in ("entry", "left", "right", "renamed_terminal"):
        builder.add_node(name, lambda state, name=name: {"visited": [name]})
    builder.add_edge(START, "entry")
    builder.add_edge("entry", "left")
    builder.add_edge("entry", "right")
    builder.add_edge(["left", "right"], "renamed_terminal")
    builder.add_edge("renamed_terminal", END)
    graph = builder.compile()
    trace = ExecutionTrace(graph, "parallel")
    trace.begin()
    for event in graph.stream({"visited": []}, stream_mode="debug"):
        if event["type"] in {"task", "task_result"}:
            trace.task(event["payload"], event["step"])
    trace.complete()
    snapshot = trace.snapshot()
    paths = snapshot["transitions"]
    assert ["left", "right"] not in paths and ["right", "left"] not in paths
    for pair in (
        ["entry", "left"],
        ["entry", "right"],
        ["left", "renamed_terminal"],
        ["right", "renamed_terminal"],
        ["renamed_terminal", "__end__"],
    ):
        assert pair in paths
    topology = snapshot["topology"]
    positions = topology["positions"]
    assert len(set(map(tuple, positions.values()))) == len(topology["nodes"])
    assert all(
        68 < x < topology["width"] - 68 and 23 < y < topology["height"] - 23
        for x, y in positions.values()
    )


def test_automatic_layout_preserves_cycles():
    from langgraph.graph import END, START, StateGraph

    from collections_lab.channels.voice_session import graph_topology

    builder = StateGraph(dict)
    builder.add_node("renamed_loop", lambda state: {})
    builder.add_edge(START, "renamed_loop")
    builder.add_conditional_edges("renamed_loop", lambda state: END, ["renamed_loop", END])
    topology = graph_topology(builder.compile())
    assert any(edge["source"] == edge["target"] == "renamed_loop" for edge in topology["edges"])
    assert set(topology["positions"]) == set(topology["nodes"])


def test_failed_start_preserves_new_call_for_audio_retry(voice_api, session, monkeypatch):
    state = api_post(voice_api, "start").json()
    api_post(voice_api, "end", state=state)
    synthesize = session.speech.synthesize

    def fail(text):
        raise RuntimeError("private-provider-secret")

    monkeypatch.setattr(session.speech, "synthesize", fail)
    response = api_post(voice_api, "start")
    assert response.status_code == 500
    current = voice_api.get("/api/session").json()
    assert current["session_id"] != state["session_id"]
    assert current["session_id"] == voice_api.get("/api/trace").json()["session_id"]
    assert len(current["history"]) == 2 and not current["finished"]
    assert "private-provider-secret" not in response.text
    monkeypatch.setattr(session.speech, "synthesize", synthesize)
    replay = api_post(voice_api, "retry", state=current).json()
    assert replay["audio_url"] and replay["history"] == current["history"]


def test_session_and_audio_reads_return_immediately_while_synthesis_runs(
    voice_api, session, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    state = finish_playback(voice_api, api_post(voice_api, "start").json())
    entered, release = Event(), Event()

    def blocked(text):
        entered.set()
        assert release.wait(5)
        return b"ID3-demo"

    monkeypatch.setattr(session.speech, "synthesize", blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(
            api_post, voice_api, "text", state=state, payload={"text": "123456789"}
        )
        try:
            assert entered.wait(3)
            current = pool.submit(voice_api.get, "/api/session").result(timeout=2).json()
            assert current["busy"] and not current["ready"]
            assert current["history"] == state["history"]
            audio = pool.submit(voice_api.get, state["audio_url"]).result(timeout=2)
            assert audio.status_code == 200 and not pending.done()
        finally:
            release.set()
        assert pending.result(timeout=3).status_code == 200
    assert not voice_api.get("/api/session").json()["busy"]


def test_deep_validation_error_is_redacted_and_failed_call_cannot_repeat_graph(
    voice_api, monkeypatch
):
    from langgraph.graph import END, START, StateGraph

    import collections_lab.channels.voice_session as module

    calls = []

    def failed_node(state):
        calls.append(True)
        raise ValueError("private-graph-secret")

    builder = StateGraph(dict)
    builder.add_node("failed_node", failed_node)
    builder.add_edge(START, "failed_node")
    builder.add_edge("failed_node", END)
    monkeypatch.setattr(module, "build_graph", lambda **kwargs: builder.compile())
    result = api_post(voice_api, "start")
    assert result.status_code == 500 and "private-graph-secret" not in result.text
    current = voice_api.get("/api/session").json()
    assert current["failed"] and not current["ready"]
    assert api_post(voice_api, "text", state=current, payload={"text": "retry"}).status_code == 400
    assert len(calls) == 1
    assert api_post(voice_api, "end", state=current).json()["finished"]


def test_graph_failure_rolls_back_agreement_and_blocks_repeated_turn(session, monkeypatch):
    offer(session)
    session.acknowledge_playback(session.audio)
    stream = session.graph.stream

    def interrupted(*args, **kwargs):
        for mode, chunk in stream(*args, **kwargs):
            yield mode, chunk
            if (
                mode == "debug"
                and chunk["type"] == "task_result"
                and chunk["payload"]["name"] == "response"
            ):
                raise RuntimeError("after committed response")

    monkeypatch.setattr(session.graph, "stream", interrupted)
    with pytest.raises(RuntimeError):
        session.send("confirmo")
    assert not session.context.repository.agreements()
    with pytest.raises(ValueError, match="Finaliza"):
        session.send("confirmo")
    assert not session.context.repository.agreements()


@pytest.mark.parametrize("failure", ["publish", "snapshot"])
def test_snapshot_failure_clears_busy_and_caches_uncertain_outcome(
    voice_api, session, monkeypatch, failure
):
    from uuid import uuid4

    state = finish_playback(voice_api, api_post(voice_api, "start").json())
    original = VoiceSession.details
    calls = 0

    def broken_details(self):
        nonlocal calls
        calls += 1
        if (failure == "publish" and calls == 3) or (failure == "snapshot" and calls >= 2):
            raise RuntimeError("private-publication-error")
        return original(self)

    key = uuid4().hex
    with monkeypatch.context() as patch:
        patch.setattr(VoiceSession, "details", broken_details)
        response = api_post(voice_api, "text", state=state, payload={"text": "123456789"}, key=key)
        assert response.status_code == 500
        assert "private-publication-error" not in response.text
        count = len(session.speech.calls)
        assert (
            api_post(voice_api, "text", state=state, payload={"text": "123456789"}, key=key).json()
            == response.json()
        )
        assert len(session.speech.calls) == count
        current = voice_api.get("/api/session").json()
        assert not current["busy"] and current["failed"] and not current["ready"]
    assert api_post(voice_api, "end", state=current).json()["finished"]


@pytest.mark.parametrize("output", [None, {}, {"reply": {"text": "missing stage"}}])
def test_invalid_stream_reply_rolls_back_and_quarantines_the_call(session, monkeypatch, output):
    offer(session)
    session.acknowledge_playback(session.audio)
    original = session.graph.stream
    history = list(session.history)
    speech_calls = len(session.speech.calls)

    def invalid_stream(*args, **kwargs):
        for mode, chunk in original(*args, **kwargs):
            if mode == "debug":
                yield mode, chunk
        if output is not None:
            yield "values", output

    monkeypatch.setattr(session.graph, "stream", invalid_stream)
    with pytest.raises(TypeError, match="invalid voice reply"):
        session.send("confirmo")
    assert session.failed and not session.trace.snapshot()["graph_complete"]
    assert not session.context.repository.agreements()
    assert session.history == history and len(session.speech.calls) == speech_calls
    with pytest.raises(ValueError, match="Finaliza"):
        session.send("confirmo")


def test_trace_eviction_does_not_invent_a_new_start_step(session):
    trace = session.trace
    trace.begin()
    entry = next(
        edge["target"] for edge in trace.topology["edges"] if edge["source"] == "__start__"
    )
    for step in range(201):
        event_id = trace.start(entry, step=step)
        trace.finish(event_id)
    result = trace.snapshot()
    assert len(result["events"]) == 200
    assert ["__start__", entry] not in result["transitions"]
    trace.begin()
    trace.start(entry, step=300)
    assert ["__start__", entry] in trace.snapshot()["transitions"]


def test_idle_trace_uses_refreshed_catalog(session, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from collections_lab.channels.voice_app import create_app

    registry, module, _, template = registry_fixture(tmp_path, monkeypatch)
    app = create_app(
        session.speech, data_dir=tmp_path / "api", now=session.context.now, graph_registry=registry
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.get("/")
        first = client.get("/api/trace").json()
        assert "alpha" in first["topology"]["nodes"]
        module.write_text(template.format(node="bravo"))
        second = client.get("/api/trace").json()
        assert "bravo" in second["topology"]["nodes"]
        assert second["graph_version"] != first["graph_version"]
        assert not second["events"] and not second["session_id"]


def test_failed_initial_publication_keeps_call_reachable_for_hangup(voice_api, monkeypatch):
    def broken_details(self):
        raise RuntimeError("publication failure")

    with monkeypatch.context() as patch:
        patch.setattr(VoiceSession, "details", broken_details)
        response = api_post(voice_api, "start")
        assert response.status_code == 500
        current = voice_api.get("/api/session").json()
        assert current["session_id"] and current["failed"] and not current["busy"]
    assert api_post(voice_api, "end", state=current).json()["finished"]


def test_trace_completion_failure_rolls_back_agreement(session, monkeypatch):
    offer(session)
    session.acknowledge_playback(session.audio)

    def fail():
        raise RuntimeError("trace completion failed")

    monkeypatch.setattr(session.trace, "complete", fail)
    with pytest.raises(RuntimeError, match="trace completion"):
        session.send("confirmo")
    assert session.failed and not session.trace.snapshot()["graph_complete"]
    assert not session.context.repository.agreements()
    with pytest.raises(ValueError, match="Finaliza"):
        session.send("confirmo")


def test_hangup_remains_finished_during_permanent_snapshot_failure(voice_api, monkeypatch):
    state = api_post(voice_api, "start").json()
    released = []
    release = VoiceSession.release_graph

    def record_release(self):
        released.append(self.thread_id)
        release(self)

    def fail(self):
        raise RuntimeError("persistent snapshot failure")

    monkeypatch.setattr(VoiceSession, "details", fail)
    monkeypatch.setattr(VoiceSession, "release_graph", record_release)
    result = api_post(voice_api, "end", state=state)
    assert result.status_code == 500
    current = voice_api.get("/api/session").json()
    assert current["finished"] and not current["busy"] and not current["ready"]
    assert state["session_id"] in released


def test_unheard_offer_cannot_use_text_as_a_playback_bypass(session, monkeypatch):
    from agents.collections import semantic

    offer(session)
    calls = []

    def accept(*args, **kwargs):
        calls.append(True)
        return semantic.TurnResult(
            semantic.TurnUnderstanding(
                intent="accept_offer", acceptance="unconditional_current_offer"
            ),
            "classified",
        )

    monkeypatch.setattr(semantic, "understand_turn", accept)
    with pytest.raises(ValueError, match="Espera"):
        session.send("terminar")
    assert not calls and not session.context.repository.agreements()
    assert session.end_call()["finished"]
    assert not calls


def test_hangup_does_not_call_semantic_or_speech_services(voice_api, session, monkeypatch):
    from agents.collections import semantic

    state = api_post(voice_api, "start").json()
    speech_calls = len(session.speech.calls)

    def unavailable(*args, **kwargs):
        raise AssertionError("Hangup must not call a model")

    monkeypatch.setattr(semantic, "understand_turn", unavailable)
    response = api_post(voice_api, "end", state=state)
    assert response.status_code == 200 and response.json()["finished"]
    assert not response.json()["audio_url"]
    assert len(session.speech.calls) == speech_calls


@pytest.mark.parametrize("suffix", [".jinja", ".jinja2"])
def test_registry_reloads_cached_jinja_templates_between_calls(tmp_path, monkeypatch, suffix):
    registry, module, _, _ = registry_fixture(tmp_path, monkeypatch)
    template = module.with_suffix(suffix)
    template.write_text("first prompt")
    module.write_text(f"""from pathlib import Path
from jinja2 import Template
from langgraph.graph import StateGraph, START, END
prompt = Template(Path(__file__).with_suffix({suffix!r}).read_text())
builder = StateGraph(dict)
builder.add_node("answer", lambda state: {{"prompt": prompt.render()}})
builder.add_edge(START, "answer")
builder.add_edge("answer", END)
agent = builder.compile()
""")
    graph, _, version = registry.acquire()
    assert graph.invoke({})["prompt"] == "first prompt"
    template.write_text("new prompt")
    assert registry.catalog()["pending_update"]
    assert graph.invoke({})["prompt"] == "first prompt"
    registry.release()
    current, _, new_version = registry.acquire()
    assert current.invoke({})["prompt"] == "new prompt" and new_version != version
    registry.release()
    template.write_text("{% invalid %}")
    catalog = registry.catalog()
    assert catalog["error"] and catalog["version"] == new_version
    retained, _, _ = registry.acquire()
    assert retained.invoke({})["prompt"] == "new prompt"
    registry.release()
    template.write_text("new prompt")
    assert not registry.catalog()["error"]


def test_registered_repository_graph_uses_one_semantic_pass_per_voice_turn(tmp_path):
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

root = Path.cwd()
data = Path(sys.argv[1])
registry = GraphRegistry(root)
context = AppContext(SQLiteRepository(data / "conversation.db"), datetime.fromisoformat("2026-10-19T09:00:00-05:00"))
call = VoiceSession(context, Speech(), data / "audio", graph_registry=registry)
from agents.collections import semantic
original = semantic.understand_turn
calls = []
def understand(*args, **kwargs):
    calls.append((args[0], kwargs["stage"]))
    return original(*args, **kwargs)
semantic.understand_turn = understand
stages = []
for text in ["Hola", "123456789", "1111", "quiero 3 cuotas desde 2026-10-20"]:
    stages.append(call.send(text)["stage"])
    nodes = [event["name"] for event in call.trace.snapshot()["events"]]
    assert nodes.count("understand_message") == 1
    assert nodes.count("route_chat_message") == 1
assert stages == ["awaiting_customer", "awaiting_identity", "ready", "awaiting_confirmation"]
assert len(calls) == 4
call.retry_audio()
assert len(calls) == 4
call.end_call()
assert len(calls) == 4 and registry._leases == 0
assert not call.context.repository.agreements()
print("registered graph + semantic turns + playback retry + hangup: OK")
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
    assert "hangup: OK" in result.stdout
