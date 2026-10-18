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
    assert session.send("terminar")["finished"]
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
