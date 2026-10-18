"""Same-origin local voice API and standalone call interface."""

import argparse
import hashlib
import json
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import RLock
from time import monotonic
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from agents.collections.memory import SQLiteRepository
from agents.collections.runtime import AppContext
from collections_lab.channels.elevenlabs import ElevenLabsSpeech, SpeechError
from collections_lab.channels.voice_session import VoiceSession
from collections_lab.config.settings import load_settings

MAX_AUDIO_BYTES = 10 * 1024 * 1024
COOKIE = "voice_browser"
AUDIO_TYPES = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/wav": ".wav"}


@dataclass
class BrowserSession:
    call: VoiceSession | None = None
    touched: float = field(default_factory=monotonic)
    started: float = 0
    ended: float | None = None
    lock: RLock = field(default_factory=RLock)
    responses: OrderedDict = field(default_factory=OrderedDict)


def create_app(speech, *, data_dir: Path, provider="mock", model="claude-haiku-4-5-20251001", now):
    app = FastAPI(
        title="Simulador de voz · Cobranza", docs_url=None, redoc_url=None, openapi_url=None
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
    browsers = {}
    registry_lock = RLock()

    def browser(request):
        with registry_lock:
            entry = browsers.get(request.cookies.get(COOKIE))
            if entry is None or monotonic() - entry.touched > 3600:
                raise HTTPException(401, "La sesión expiró. Recarga la página para comenzar.")
            entry.touched = monotonic()
            return entry

    def snapshot(entry):
        call = entry.call
        if call is None:
            return {"session_id": None, "history": [], "finished": False, "ready": False}
        audio_id = Path(call.audio).name if call.audio else None
        return {
            "session_id": call.thread_id,
            "elapsed_seconds": max(0, (entry.ended or monotonic()) - entry.started),
            "history": list(call.history),
            "finished": call.reply["finished"],
            "ready": not call.reply["finished"]
            and (
                call.played_audio == call.audio
                if call.audio
                else call.reply["stage"] != "awaiting_confirmation"
            ),
            "audio_id": audio_id,
            "audio_url": f"/api/audio/{audio_id}" if audio_id else None,
            "warning": call.warning,
            "details": call.details(),
            "provider": provider,
        }

    @app.middleware("http")
    async def local_boundary(request, call_next):
        if request.method == "POST":
            origin = request.headers.get("origin")
            if (origin and origin != str(request.base_url).rstrip("/")) or (
                request.headers.get("x-voice-client") != "1"
            ):
                return JSONResponse(
                    {"detail": "Solicitud de otro origen rechazada."}, status_code=403
                )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'"
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        with registry_lock:
            for key, value in list(browsers.items()):
                if monotonic() - value.touched > 3600:
                    del browsers[key]
            key = request.cookies.get(COOKIE)
            if key not in browsers:
                if len(browsers) >= 64:
                    raise HTTPException(429, "Demasiadas sesiones locales. Intenta más tarde.")
                key = uuid4().hex
                browsers[key] = BrowserSession()
        response = HTMLResponse(Path(__file__).with_name("voice.html").read_text())
        response.set_cookie(COOKIE, key, httponly=True, samesite="strict", max_age=3600)
        return response

    @app.get("/api/session")
    def current(request: Request):
        entry = browser(request)
        with entry.lock:
            return snapshot(entry)

    @app.get("/api/audio/{audio_id}")
    def audio(audio_id: str, request: Request):
        entry = browser(request)
        with entry.lock:
            call = entry.call
            if not call or not call.audio or Path(call.audio).name != audio_id:
                raise HTTPException(404, "Audio no disponible para esta sesión.")
            return FileResponse(call.audio, media_type="audio/mpeg")

    def perform(entry, action, raw, request_id, call_id, content_type):
        digest = hashlib.sha256(action.encode() + call_id.encode() + raw).hexdigest()
        with entry.lock:
            if request_id in entry.responses:
                previous_digest, result, status = entry.responses[request_id]
                if previous_digest != digest:
                    return JSONResponse({"detail": "Identificador de solicitud reutilizado."}, 409)
                return JSONResponse(result, status)
            status = 200
            try:
                payload = {} if action == "recording" else json.loads(raw or b"{}")
                if not isinstance(payload, dict):
                    raise TypeError("Mensaje inválido.")
                call = entry.call
                if action == "start":
                    if call and not call.reply["finished"]:
                        raise ValueError("Ya hay una llamada activa.")
                    directory = Path(data_dir) / uuid4().hex
                    context = AppContext(
                        SQLiteRepository(directory / "conversation.sqlite3"), now, provider, model
                    )
                    call = VoiceSession(context, speech, directory)
                    entry.started, entry.ended = monotonic(), None
                    call.send("Hola")
                    entry.call = call
                else:
                    if call is None or call.thread_id != call_id:
                        raise ValueError("La llamada cambió. Recarga la página.")
                    if action in {"text", "recording"} and not snapshot(entry)["ready"]:
                        raise ValueError("Espera a que termine el mensaje del agente.")
                    if action == "text":
                        text = payload.get("text")
                        if not isinstance(text, str):
                            raise ValueError("Escribe una respuesta.")
                        call.send(text)
                    elif action == "recording":
                        path = call.directory / ("input-" + uuid4().hex + AUDIO_TYPES[content_type])
                        try:
                            path.write_bytes(raw)
                            call.respond_to_audio(path)
                        finally:
                            path.unlink(missing_ok=True)
                    elif action == "playback":
                        audio_id = payload.get("audio_id")
                        if not isinstance(audio_id, str) or audio_id != snapshot(entry)["audio_id"]:
                            raise ValueError("Ese audio ya no corresponde a la respuesta actual.")
                        call.acknowledge_playback(audio_id)
                    elif action == "retry":
                        call.retry_audio()
                    elif action == "end" and not call.reply["finished"]:
                        call.send("terminar", speak=False)
                if entry.call and entry.call.reply["finished"] and entry.ended is None:
                    entry.ended = monotonic()
                result = snapshot(entry)
            except (TypeError, ValueError, SpeechError) as error:
                result, status = {"detail": str(error), "state": snapshot(entry)}, 400
            except Exception:  # noqa: BLE001 - cache uncertain outcomes at the API boundary
                # Cache an uncertain failure too: replaying must never repeat business writes.
                result, status = (
                    {
                        "detail": "No se pudo completar el turno. Revisa el estado de la llamada.",
                        "state": snapshot(entry),
                    },
                    500,
                )
            entry.responses[request_id] = (digest, result, status)
            if len(entry.responses) > 128:
                entry.responses.popitem(last=False)
            return JSONResponse(result, status)

    @app.post("/api/{action}")
    async def mutate(action: str, request: Request):
        if action not in {"start", "text", "recording", "playback", "retry", "end"}:
            raise HTTPException(404, "Acción desconocida.")
        entry = browser(request)
        request_id = request.headers.get("x-request-id", "")
        if not re.fullmatch(r"[a-f0-9-]{32,36}", request_id):
            raise HTTPException(400, "Falta el identificador de solicitud.")
        content_type = request.headers.get("content-type", "").split(";")[0]
        if action == "recording" and content_type not in AUDIO_TYPES:
            raise HTTPException(415, "Formato de audio no compatible.")
        limit = MAX_AUDIO_BYTES if action == "recording" else 20000
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > limit:
                raise HTTPException(
                    413,
                    "La grabación supera el límite de 10 MB."
                    if action == "recording"
                    else "Mensaje demasiado largo.",
                )
            raw.extend(chunk)
        if action == "recording" and not raw:
            raise HTTPException(400, "No se recibió audio.")
        return await run_in_threadpool(
            perform,
            entry,
            action,
            bytes(raw),
            request_id,
            request.headers.get("x-call-id", ""),
            content_type,
        )

    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--data-dir", type=Path, default=Path(".local/voice"))
    parser.add_argument("--provider", choices=["mock", "anthropic"])
    args = parser.parse_args()
    settings = load_settings(Path.cwd() / ".env")
    speech = ElevenLabsSpeech(
        os.getenv("ELEVENLABS_API_KEY", ""), os.getenv("ELEVENLABS_VOICE_ID", "")
    )
    app = create_app(
        speech,
        data_dir=args.data_dir,
        provider=args.provider or settings.llm_provider,
        model=settings.anthropic_model,
        now=datetime.fromisoformat(settings.demo_now),
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
