"""Voice turns around the existing graph, gated by current-offer playback completion."""

import json
from pathlib import Path
from threading import RLock
from time import perf_counter
from uuid import uuid4

from langchain_core.messages import HumanMessage

from agents.collections.agent import build_graph
from agents.collections.conversation import requests_end
from collections_lab.channels.elevenlabs import SpeechError


class VoiceSession:
    def __init__(self, context, speech, directory: Path):
        self.context = context
        self.speech = speech
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.thread_id = uuid4().hex
        self.graph = build_graph(studio=True)
        self.history = []
        self.reply = None
        self.audio = None
        self.played_audio = None
        self.warning = ""
        self.latencies = {}
        self._lock = RLock()

    def transcribe(self, path):
        with self._lock:
            started = perf_counter()
            text = self.speech.transcribe(path)
            self.latencies["stt_seconds"] = round(perf_counter() - started, 3)
            return text

    def acknowledge_playback(self, audio_path):
        """Accept the browser ended event only for the current generated audio file."""
        with self._lock:
            if not audio_path or not self.audio or Path(audio_path).name != Path(self.audio).name:
                return False
            if self.played_audio == self.audio:
                return False
            self.played_audio = self.audio
            with (self.directory / "playback-acknowledgements.jsonl").open("a") as stream:
                stream.write(
                    json.dumps(
                        {
                            "thread_id": self.thread_id,
                            "message_id": self.reply.get("message_id"),
                            "offer_id": self.reply.get("offer_id"),
                            "audio": Path(self.audio).name,
                            "method": "browser_audio_ended",
                        }
                    )
                    + "\n"
                )
            return True

    def check_turn(self, text=""):
        if self.reply and self.reply["finished"]:
            raise ValueError("Esta sesión terminó. Inicia una nueva conversación.")
        pending = self.reply and self.reply["stage"] == "awaiting_confirmation"
        if (
            pending
            and not requests_end(text)
            and (not self.audio or self.played_audio != self.audio)
        ):
            raise ValueError(
                "Espera a que termine la oferta antes de responder. "
                "Si no suena, pulsa Escuchar mensaje; si falló, pulsa Reintentar audio."
            )

    def respond_to_audio(self, path):
        with self._lock:
            self.check_turn()  # Do not spend transcription credits on a blocked turn.
            return self.send(self.transcribe(path))

    def send(self, text: str, *, speak=True):
        with self._lock:
            text = text.strip()
            if not text or len(text) > 4000:
                raise ValueError("Escribe o graba un mensaje de entre 1 y 4000 caracteres.")
            self.check_turn(text)
            started = perf_counter()
            with self.context.repository.connection():
                result = self.graph.invoke(
                    {"messages": [HumanMessage(content=text, id=uuid4().hex)]},
                    config={"configurable": {"thread_id": self.thread_id}},
                    context=self.context,
                )
            self.reply = result["reply"]
            self.latencies["agent_seconds"] = round(perf_counter() - started, 3)
            self.history.extend(
                [
                    {"role": "user", "content": text},
                    {"role": "assistant", "content": self.reply["text"]},
                ]
            )
            # Persist the turn before synthesis. An audio retry never replays the graph.
            if speak:
                self.retry_audio()
            else:
                self.audio = self.played_audio = None
                self.warning = ""
            return self.reply

    def retry_audio(self):
        with self._lock:
            if self.reply is None:
                raise ValueError("Inicia una sesión primero.")
            self.audio = None
            self.played_audio = None
            self.warning = ""
            started = perf_counter()
            try:
                data = self.speech.synthesize(self.reply["text"])
                path = self.directory / f"reply-{uuid4().hex}.mp3"
                path.write_bytes(data)
                self.audio = str(path)
            except (SpeechError, OSError) as error:
                self.warning = (
                    str(error) if isinstance(error, SpeechError) else "No se pudo guardar el audio."
                ) + " El turno ya está guardado; usa Reintentar audio."
            self.latencies["tts_seconds"] = round(perf_counter() - started, 3)
            return self.audio

    def details(self):
        return {
            "stage": self.reply["stage"] if self.reply else "not_started",
            "offer_id": self.reply.get("offer_id") if self.reply else None,
            "agreement_id": self.reply.get("agreement_id") if self.reply else None,
            "latency": dict(self.latencies),
            "playback_check": "browser ended event; no audibility verification",
        }
