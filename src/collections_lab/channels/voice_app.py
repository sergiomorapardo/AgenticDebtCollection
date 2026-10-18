"""Local Gradio voice simulator with automatic transcription, sending, and playback."""

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
            raise gr.Error("La sesión expiró. Recarga la página e inicia una conversación.")
        return sessions[identifier]

    def ready(session):
        if session.reply["finished"]:
            return False
        if session.audio:
            return session.played_audio == session.audio
        return session.reply["stage"] != "awaiting_confirmation"

    def render(session, *, replace_audio=True, error=None):
        finished = session.reply["finished"]
        can_respond = ready(session)
        message = (
            error
            or session.warning
            or (
                "Conversación terminada. Puedes iniciar otra."
                if finished
                else "Tu turno: graba tu respuesta y detén la grabación para enviarla."
                if can_respond
                else "Escucha al agente. Si no suena automáticamente, pulsa reproducir."
            )
        )
        return (
            list(session.history),
            gr.update(value=session.audio, visible=True) if replace_audio else gr.skip(),
            session.details(),
            message,
            gr.update(visible=finished, value="Iniciar otra conversación"),
            gr.update(value=None, visible=can_respond, interactive=True),
            gr.update(interactive=not finished),
            gr.update(value="", interactive=can_respond),
            gr.update(value=None, interactive=can_respond),
        )

    def start(identifier):
        if identifier in sessions and not sessions[identifier].reply["finished"]:
            raise gr.Error("Termina la conversación actual antes de iniciar otra.")
        identifier = uuid4().hex
        directory = data_dir / identifier
        context = AppContext(
            SQLiteRepository(directory / "conversation.sqlite3"), now, provider, model
        )
        session = VoiceSession(context, speech, directory)
        session.send("Hola")
        sessions[identifier] = session
        return (identifier, *render(session))

    def respond(identifier, recording):
        session = get_session(identifier)
        try:
            if not recording:
                raise ValueError("No se recibió audio. Graba de nuevo o escribe en Opciones.")
            session.respond_to_audio(recording)
        except (SpeechError, ValueError) as error:
            return render(session, replace_audio=False, error=str(error))
        return render(session)

    def send(identifier, text):
        session = get_session(identifier)
        try:
            session.send(text)
        except ValueError as error:
            raise gr.Error(str(error)) from None
        return render(session)

    def playback_finished(identifier, audio_path):
        session = get_session(identifier)
        if not session.acknowledge_playback(audio_path):
            return tuple(gr.skip() for _ in outputs)
        return render(session, replace_audio=False)

    def end(identifier):
        session = get_session(identifier)
        session.send("terminar")
        return render(session)

    def retry(identifier):
        session = get_session(identifier)
        session.retry_audio()
        return render(session)

    with gr.Blocks(title="Simulador de voz · Cobranza", analytics_enabled=False) as app:
        identifier = gr.State(
            None, time_to_live=3600, delete_callback=lambda key: sessions.pop(key, None)
        )
        gr.Markdown(
            "# Hablemos\n"
            "Escucha al agente y graba tu respuesta. Al detener la grabación, se envía sola.\n\n"
            "Demo con datos ficticios. El audio se envía a ElevenLabs y consume créditos."
        )
        start_button = gr.Button("Iniciar conversación", variant="primary")
        status = gr.Markdown("Inicia la conversación para escuchar el saludo del agente.")
        audio = gr.Audio(
            label="Agente",
            autoplay=True,
            interactive=False,
            type="filepath",
            visible=False,
            buttons=[],
        )
        recording = gr.Audio(
            sources=["microphone"],
            type="filepath",
            label="Responder",
            visible=False,
            interactive=True,
            editable=False,
            buttons=[],
        )
        history = gr.Chatbot(label="Conversación", height=300, buttons=[])
        with gr.Accordion("Opciones · escribir, cargar audio o terminar", open=False):
            text = gr.Textbox(
                label="Responder por escrito",
                placeholder="Escribe y pulsa Enter",
                interactive=False,
            )
            upload = gr.Audio(
                sources=["upload"],
                type="filepath",
                label="Cargar y enviar audio",
                interactive=False,
                editable=False,
                buttons=[],
            )
            with gr.Row():
                retry_button = gr.Button("Reintentar audio del agente")
                end_button = gr.Button("Terminar conversación", interactive=False)
            gr.Markdown(
                "Datos de prueba: cédula `1000000001`, código `1111`. "
                "Puedes pedir `quiero 3 cuotas desde 2026-10-20` y luego `confirmo`.\n\n"
                f"Conversación: `{provider}` · Reloj de demo: `{now.isoformat()}`"
            )
            details = gr.JSON(label="Estado y latencia")
        outputs = [
            history,
            audio,
            details,
            status,
            start_button,
            recording,
            end_button,
            text,
            upload,
        ]
        events = {"api_visibility": "private", "concurrency_id": "voice", "concurrency_limit": 1}
        start_button.click(start, [identifier], [identifier, *outputs], **events)
        recording.stop_recording(respond, [identifier, recording], outputs, **events)
        upload.upload(respond, [identifier, upload], outputs, **events)
        text.submit(send, [identifier, text], outputs, **events)
        audio.stop(playback_finished, [identifier, audio], outputs, **events)
        end_button.click(end, [identifier], outputs, **events)
        retry_button.click(retry, [identifier], outputs, **events)
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
