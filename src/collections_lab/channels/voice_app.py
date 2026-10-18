"""Local Gradio voice simulator. No public sharing or telephone calls."""

import argparse
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from agents.collections.memory import SQLiteRepository
from agents.collections.runtime import AppContext
from collections_lab.channels.elevenlabs import ElevenLabsSpeech, SpeechError
from collections_lab.channels.voice_session import VoiceSession
from collections_lab.config.settings import load_settings


def create_app(speech, *, data_dir: Path, provider="mock", model="claude-haiku-4-5-20251001", now):
    import gradio as gr

    sessions = {}

    def get_session(identifier):
        if identifier not in sessions:
            raise gr.Error("Inicia una sesión. Si expiró, recarga la página.")
        return sessions[identifier]

    def render(session):
        finished = session.reply["finished"]
        pending = session.reply["stage"] == "awaiting_confirmation" and not finished
        return (
            list(session.history),
            session.audio,
            session.details(),
            session.warning or "Turno listo. Puedes reproducir el audio.",
            gr.update(value=False, visible=pending),
            gr.update(interactive=finished),
            gr.update(interactive=not finished),
            gr.update(interactive=not finished),
            "",
            None,
        )

    def start(identifier):
        if identifier in sessions and not sessions[identifier].reply["finished"]:
            raise gr.Error("Termina la sesión actual antes de iniciar otra.")
        identifier = uuid4().hex
        directory = data_dir / identifier
        context = AppContext(
            SQLiteRepository(directory / "conversation.sqlite3"), now, provider, model
        )
        session = VoiceSession(context, speech, directory)
        session.send("Hola")
        sessions[identifier] = session
        return (identifier, *render(session))

    def transcribe(identifier, recording):
        session = get_session(identifier)
        if not recording:
            raise gr.Error("Graba o carga un audio primero.")
        try:
            text = session.transcribe(recording)
            return text, session.details(), "Revisa la transcripción y pulsa Enviar turno."
        except SpeechError as error:
            raise gr.Error(str(error)) from None

    def send(identifier, text, heard):
        session = get_session(identifier)
        try:
            session.send(text, heard_offer=heard)
        except ValueError as error:
            raise gr.Error(str(error)) from None
        return render(session)

    def end(identifier):
        session = get_session(identifier)
        session.send("terminar")
        return render(session)

    def retry(identifier):
        session = get_session(identifier)
        session.retry_audio()
        return session.audio, session.warning or "Audio listo.", session.details(), False

    with gr.Blocks(title="Simulador de voz · Cobranza", analytics_enabled=False) as app:
        identifier = gr.State(
            None, time_to_live=3600, delete_callback=lambda key: sessions.pop(key, None)
        )
        gr.Markdown(
            "# Simulador de voz\n"
            "Demo con datos ficticios, por turnos y sin llamadas telefónicas. "
            "El audio se envía a ElevenLabs y consume créditos.\n\n"
            f"**Conversación:** {provider} · **Reloj de demo:** {now.isoformat()}\n\n"
            "Prueba: `Hola` → `Mi cédula es 1000000001` → `verificar 1111` → "
            "`quiero 3 cuotas desde 2026-10-20` → `confirmo`. "
            "Segunda cuenta: `1000000002`, código `2222`. Usa solo datos ficticios."
        )
        with gr.Row():
            start_button = gr.Button("Iniciar sesión", variant="primary")
            end_button = gr.Button("Terminar sesión", interactive=False)
        history = gr.Chatbot(label="Conversación", height=330)
        with gr.Row():
            recording = gr.Audio(
                sources=["microphone", "upload"], type="filepath", label="Tu mensaje"
            )
            audio = gr.Audio(label="Respuesta del agente", autoplay=False, interactive=False)
        transcribe_button = gr.Button("Transcribir audio")
        text = gr.Textbox(
            label="Transcripción / mensaje",
            placeholder="Revisa el texto antes de enviarlo.",
            lines=2,
        )
        heard = gr.Checkbox(
            label="Escuché completa esta oferta, incluidos montos y fechas",
            value=False,
            visible=False,
        )
        send_button = gr.Button("Enviar turno", variant="primary", interactive=False)
        retry_button = gr.Button("Reintentar audio")
        status = gr.Markdown(
            "Inicia una sesión para comenzar. Cada nueva sesión usa una base de datos aislada."
        )
        details = gr.JSON(label="Estado y latencia")
        outputs = [
            history,
            audio,
            details,
            status,
            heard,
            start_button,
            send_button,
            end_button,
            text,
            recording,
        ]
        events = {"api_visibility": "private", "concurrency_id": "voice", "concurrency_limit": 1}
        start_button.click(start, [identifier], [identifier, *outputs], **events)
        transcribe_button.click(
            transcribe, [identifier, recording], [text, details, status], **events
        )
        send_button.click(send, [identifier, text, heard], outputs, **events)
        end_button.click(end, [identifier], outputs, **events)
        retry_button.click(retry, [identifier], [audio, status, details, heard], **events)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--data-dir", type=Path, default=Path(".local/voice"))
    parser.add_argument("--provider", choices=["mock", "anthropic"])
    args = parser.parse_args()
    settings = load_settings(Path.cwd() / ".env")
    speech = ElevenLabsSpeech(
        os.getenv("ELEVENLABS_API_KEY", ""), os.getenv("ELEVENLABS_VOICE_ID", "")
    )
    # Reviewed wording reaches speech; speculative token streaming is not spoken.
    app = create_app(
        speech,
        data_dir=args.data_dir,
        provider=args.provider or settings.llm_provider,
        model=settings.anthropic_model,
        now=datetime.fromisoformat(settings.demo_now),
    )
    app.queue().launch(
        server_name="127.0.0.1", server_port=args.port, share=False, max_file_size="10mb"
    )


if __name__ == "__main__":
    main()
