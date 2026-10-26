"""Small, bounded ElevenLabs adapters; credentials never reach the browser."""

import re
from datetime import date
from pathlib import Path

import httpx

from agents.collections.numbers import normalize_transcript_numbers


class SpeechError(RuntimeError):
    """A speech request failed without exposing provider payloads or credentials."""


def _spanish_integer(value: int) -> str:
    """Spell non-negative integers before masculine currency/scale nouns."""
    small = (
        "cero",
        "un",
        "dos",
        "tres",
        "cuatro",
        "cinco",
        "seis",
        "siete",
        "ocho",
        "nueve",
        "diez",
        "once",
        "doce",
        "trece",
        "catorce",
        "quince",
        "dieciséis",
        "diecisiete",
        "dieciocho",
        "diecinueve",
        "veinte",
        "veintiún",
        "veintidós",
        "veintitrés",
        "veinticuatro",
        "veinticinco",
        "veintiséis",
        "veintisiete",
        "veintiocho",
        "veintinueve",
    )
    if not 0 <= value < 10**18:
        raise SpeechError("El importe supera el rango admitido para pronunciación.")
    if value < 30:
        return small[value]
    if value < 100:
        tens, units = divmod(value, 10)
        word = (
            "",
            "",
            "",
            "treinta",
            "cuarenta",
            "cincuenta",
            "sesenta",
            "setenta",
            "ochenta",
            "noventa",
        )[tens]
        return word + (f" y {small[units]}" if units else "")
    if value == 100:
        return "cien"
    if value < 1000:
        hundreds, remainder = divmod(value, 100)
        word = (
            "",
            "ciento",
            "doscientos",
            "trescientos",
            "cuatrocientos",
            "quinientos",
            "seiscientos",
            "setecientos",
            "ochocientos",
            "novecientos",
        )[hundreds]
        return word + (f" {_spanish_integer(remainder)}" if remainder else "")
    for scale, singular, plural in (
        (10**12, "billón", "billones"),
        (10**6, "millón", "millones"),
        (1000, "mil", "mil"),
    ):
        if value >= scale:
            count, remainder = divmod(value, scale)
            word = (
                "mil"
                if scale == 1000 and count == 1
                else f"{_spanish_integer(count)} {singular if count == 1 else plural}"
            )
            return word + (f" {_spanish_integer(remainder)}" if remainder else "")
    raise AssertionError("Unreachable integer range")


def _spoken_currency(raw: str) -> str:
    if re.fullmatch(r"\d{1,3}([.,])\d{3}(?:\1\d{3})*", raw):
        raw = raw.replace(",", "").replace(".", "")
    decimal = re.fullmatch(r"(\d+)[.,](\d{1,2})", raw)
    if decimal:
        whole, fraction = decimal.groups()
        cents = int(fraction.ljust(2, "0"))
    elif raw.isdecimal():
        whole, cents = raw, 0
    else:
        raise SpeechError("El formato del importe es ambiguo para pronunciación.")
    value = int(whole)
    noun = "peso" if value == 1 else "pesos"
    link = " de" if value and value % 10**6 == 0 else ""
    words = f"{_spanish_integer(value)}{link} {noun}"
    if cents:
        words += f" con {_spanish_integer(cents)} {'centavo' if cents == 1 else 'centavos'}"
    return words


def _spoken_date(raw: str) -> str:
    """Render a validated ISO date as natural Colombian Spanish speech."""
    try:
        value = date.fromisoformat(raw)
    except ValueError as exc:
        raise SpeechError("La fecha no es válida para pronunciación.") from exc
    months = (
        "enero",
        "febrero",
        "marzo",
        "abril",
        "mayo",
        "junio",
        "julio",
        "agosto",
        "septiembre",
        "octubre",
        "noviembre",
        "diciembre",
    )

    def cardinal(number):
        words = _spanish_integer(number)
        return re.sub(r"veintiún$", "veintiuno", re.sub(r"\bun$", "uno", words))

    day = "primero" if value.day == 1 else cardinal(value.day)
    return f"{day} de {months[value.month - 1]} de {cardinal(value.year)}"


def _spoken_text(text: str) -> str:
    """Render currency and dates naturally at TTS, retaining written responses."""
    if not text.strip() or len(text) > 4000:
        raise SpeechError("La respuesta debe tener entre 1 y 4000 caracteres.")

    def amount(match: re.Match) -> str:
        return _spoken_currency(match[1])

    text = re.sub(r"(?<![\w.,])\$?(\d+(?:[.,]\d+)*)\s+COP\b", amount, text, flags=re.IGNORECASE)
    text = re.sub(r"\bCOP\s+\$?(\d+(?:[.,]\d+)*)", amount, text, flags=re.IGNORECASE)
    text = re.sub(r"\bCOP\b", "pesos", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?<![\w-])\d{4}-\d{2}-\d{2}(?![\w-])", lambda match: _spoken_date(match[0]), text
    )
    if len(text) > 4000:
        raise SpeechError("La respuesta hablada supera los 4000 caracteres.")
    return text


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
        text = _spoken_text(text)
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

    async def stream_synthesize(self, text: str):
        """Yield mono signed 16-bit little-endian PCM at 24 kHz without buffering an MP3."""
        # Keep the model accepted in speaker tests; Flash clipped this voice's opening phoneme.
        text = _spoken_text(text)
        try:
            async with (
                httpx.AsyncClient(
                    base_url="https://api.elevenlabs.io/v1/",
                    headers={"xi-api-key": self._api_key},
                    timeout=60,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._transport,
                ) as client,
                client.stream(
                    "POST",
                    f"text-to-speech/{self.voice_id}/stream",
                    params={"output_format": "pcm_24000"},
                    json={
                        "text": text,
                        "model_id": "eleven_multilingual_v2",
                        "language_code": "es",
                    },
                ) as response,
            ):
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if not content_type.startswith(("audio/", "application/octet-stream")):
                    raise SpeechError("ElevenLabs no devolvió audio válido.")
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 24_000 * 2 * 300:
                        raise SpeechError("El audio supera el límite de cinco minutos.")
                    if chunk:
                        yield chunk
                if not size or size % 2:
                    raise SpeechError("ElevenLabs devolvió audio incompleto.")
        except httpx.HTTPStatusError as error:
            raise SpeechError(
                f"ElevenLabs devolvió HTTP {error.response.status_code}. Revisa permisos y créditos."
            ) from None
        except httpx.RequestError:
            raise SpeechError("No se pudo conectar con ElevenLabs. Puedes reintentar.") from None

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
        return normalize_transcript_numbers(text)
