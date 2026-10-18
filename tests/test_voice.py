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


def test_gradio_app_constructs_without_network_or_speech_calls(session, tmp_path):
    pytest.importorskip("gradio")
    from collections_lab.channels.voice_app import create_app

    app = create_app(session.speech, data_dir=tmp_path, now=session.context.now)
    assert app.title == "Simulador de voz · Cobranza"
    assert not session.speech.calls
    handlers = {function.name: function.fn for function in app.fns.values()}
    identifier, *view = handlers["start"](None)
    assert len(session.speech.calls) == 1
    audio_path = view[1]["value"]
    assert not view[5]["visible"]  # Reply control waits for the response to finish.
    completed = handlers["playback_finished"](identifier, audio_path)
    assert completed[5]["visible"]
    responded = handlers["respond"](identifier, "synthetic.wav")
    assert session.speech.transcriptions == ["synthetic.wav"]
    assert len(responded[0]) == 4
    assert not responded[5]["visible"]
    assert responded[1]["value"] != audio_path
    stale = handlers["playback_finished"](identifier, audio_path)
    assert all(update == {"__type__": "update"} for update in stale)


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
