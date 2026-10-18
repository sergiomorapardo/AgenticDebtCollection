"""Manual voice turns around the existing graph, with explicit offer acknowledgement."""

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
        self.warning = ""
        self.latencies = {}
        self._lock = RLock()

    def transcribe(self, path):
        with self._lock:
            started = perf_counter()
            text = self.speech.transcribe(path)
            self.latencies["stt_seconds"] = round(perf_counter() - started, 3)
            return text

    def send(self, text: str, *, heard_offer=False):
        with self._lock:
            text = text.strip()
            if not text or len(text) > 4000:
                raise ValueError("Escribe o transcribe un mensaje de entre 1 y 4000 caracteres.")
            if self.reply and self.reply["finished"]:
                raise ValueError("Esta sesión terminó. Inicia una nueva simulación.")
            pending = self.reply and self.reply["stage"] == "awaiting_confirmation"
            if pending and not requests_end(text):
                if not self.audio or not heard_offer:
                    raise ValueError(
                        "Escucha completa la oferta y marca la casilla antes de continuar. "
                        "Si el audio falló, reinténtalo. Siempre puedes terminar la sesión."
                    )
                # Bind the acknowledgement to this exact reply, not to a future offer.
                with (self.directory / "playback-acknowledgements.jsonl").open("a") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "thread_id": self.thread_id,
                                "message_id": self.reply.get("message_id"),
                                "offer_id": self.reply.get("offer_id"),
                                "audio": Path(self.audio).name,
                                "method": "manual_full_offer_acknowledgement",
                            }
                        )
                        + "\n"
                    )
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
            self.retry_audio()
            return self.reply

    def retry_audio(self):
        with self._lock:
            if self.reply is None:
                raise ValueError("Inicia una sesión primero.")
            self.audio = None
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
            "playback_check": "manual acknowledgement; no automatic audibility verification",
        }
