"""Small, bounded ElevenLabs adapters; credentials never reach the browser."""

import re
from pathlib import Path

import httpx


class SpeechError(RuntimeError):
    """A speech request failed without exposing provider payloads or credentials."""


class ElevenLabsSpeech:
    def __init__(self, api_key: str, voice_id: str, *, transport=None):
        if not api_key or not re.fullmatch(r"[A-Za-z0-9_-]+", voice_id):
            raise ValueError("Configure ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID in local .env.")
        self._api_key = api_key
        self.voice_id = voice_id
        self._transport = transport

    def _request(self, path, **kwargs):
        try:
            with httpx.Client(
                base_url="https://api.elevenlabs.io/v1/",
                headers={"xi-api-key": self._api_key},
                timeout=60,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                response = client.post(path, **kwargs)
                response.raise_for_status()
                return response
        except httpx.HTTPStatusError as error:
            raise SpeechError(
                f"ElevenLabs devolvió HTTP {error.response.status_code}. "
                "Revisa permisos y créditos. No se reintentó automáticamente."
            ) from None
        except httpx.RequestError:
            raise SpeechError("No se pudo conectar con ElevenLabs. Puedes reintentar.") from None

    def synthesize(self, text: str) -> bytes:
        if not text.strip() or len(text) > 4000:
            raise SpeechError("La respuesta debe tener entre 1 y 4000 caracteres.")
        response = self._request(
            f"text-to-speech/{self.voice_id}",
            params={"output_format": "mp3_44100_128"},
            json={"text": text, "model_id": "eleven_multilingual_v2"},
        )
        if (
            not response.headers.get("content-type", "").startswith("audio/")
            or not response.content
        ):
            raise SpeechError("ElevenLabs no devolvió audio válido.")
        return response.content

    def transcribe(self, path: str | Path) -> str:
        path = Path(path)
        if not path.is_file() or not 0 < path.stat().st_size <= 10 * 1024 * 1024:
            raise SpeechError("Selecciona una grabación de entre 1 byte y 10 MB.")
        with path.open("rb") as audio:
            response = self._request(
                "speech-to-text",
                files={"file": (path.name, audio, "application/octet-stream")},
                data={
                    "model_id": "scribe_v2",
                    "language_code": "es",
                    "tag_audio_events": "false",
                    "diarize": "false",
                },
            )
        try:
            text = response.json()["text"].strip()
        except (ValueError, KeyError, TypeError, AttributeError):
            raise SpeechError("ElevenLabs devolvió una transcripción inválida.") from None
        if not text or len(text) > 4000:
            raise SpeechError("No se detectó texto válido de entre 1 y 4000 caracteres.")
        return text
