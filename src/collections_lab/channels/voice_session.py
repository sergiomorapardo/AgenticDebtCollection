"""Voice turns around the existing graph, gated by current-offer playback completion."""

import json
from copy import deepcopy
from pathlib import Path
from threading import RLock
from time import perf_counter
from uuid import uuid4

from langchain_core.messages import HumanMessage

from agents.collections.agent import build_graph
from agents.collections.conversation import requests_end
from collections_lab.channels.elevenlabs import SpeechError


class ExecutionTrace:
    """Bounded, sanitized execution evidence readable without the business-turn lock."""

    def __init__(self, graph, session_id):
        topology = graph.get_graph()
        self.topology = {
            "nodes": list(topology.nodes),
            "edges": [
                {"source": edge.source, "target": edge.target, "conditional": edge.conditional}
                for edge in topology.edges
            ],
        }
        self.session_id = session_id
        self.turn = 0
        self.events = []
        self.revision = 0
        self._lock = RLock()

    def begin(self):
        with self._lock:
            self.turn += 1
            self.events = []
            self.revision += 1

    def start(self, name, *, event_id=None, kind="node"):
        with self._lock:
            event_id = event_id or uuid4().hex
            self.events.append(
                {
                    "id": event_id,
                    "name": name,
                    "kind": kind,
                    "status": "running",
                    "started": perf_counter(),
                    "duration_ms": None,
                    "summary": {},
                }
            )
            self.events = self.events[-200:]
            self.revision += 1
            return event_id

    def finish(self, event_id, *, failed=False, summary=None):
        with self._lock:
            for event in reversed(self.events):
                if event["id"] == event_id:
                    event["status"] = "error" if failed else "complete"
                    event["duration_ms"] = round((perf_counter() - event["started"]) * 1000, 1)
                    event["summary"] = summary or {}
                    self.revision += 1
                    return

    def task(self, event):
        if event["name"] not in self.topology["nodes"]:
            return
        if "result" not in event and "error" not in event:
            self.start(event["name"], event_id=event["id"])
        else:
            # Never expose raw state, prompts, identifiers, transcripts, or exception text.
            output = event.get("result") or {}
            summary = {"updated_fields": sorted(output)} if isinstance(output, dict) else {}
            reply = (
                output.get("reply") or output.get("chat_reply")
                if isinstance(output, dict)
                else None
            )
            if isinstance(reply, dict):
                summary["finished"] = bool(reply.get("finished"))
            self.finish(event["id"], failed=event.get("error") is not None, summary=summary)

    def fail_running(self):
        with self._lock:
            for event in self.events:
                if event["status"] == "running":
                    self.finish(event["id"], failed=True)

    def snapshot(self):
        with self._lock:
            events = deepcopy(self.events)
            for event in events:
                started = event.pop("started")
                if event["status"] == "running":
                    event["duration_ms"] = round((perf_counter() - started) * 1000, 1)
            return {
                "session_id": self.session_id,
                "turn": self.turn,
                "revision": self.revision,
                "topology": self.topology,
                "events": events,
            }


class VoiceSession:
    def __init__(self, context, speech, directory: Path):
        self.context = context
        self.speech = speech
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.thread_id = uuid4().hex
        self.graph = build_graph(studio=True)
        self.trace = ExecutionTrace(self.graph, self.thread_id)
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
            event_id = self.trace.start("speech_to_text", kind="speech")
            try:
                text = self.speech.transcribe(path)
            except Exception:
                self.trace.finish(event_id, failed=True)
                raise
            self.trace.finish(event_id)
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
            self.trace.begin()
            return self.send(self.transcribe(path), new_turn=False)

    def send(self, text: str, *, speak=True, new_turn=True):
        with self._lock:
            text = text.strip()
            if not text or len(text) > 4000:
                raise ValueError("Escribe o graba un mensaje de entre 1 y 4000 caracteres.")
            self.check_turn(text)
            if new_turn:
                self.trace.begin()
            started = perf_counter()
            try:
                with self.context.repository.connection():
                    for mode, chunk in self.graph.stream(
                        {"messages": [HumanMessage(content=text, id=uuid4().hex)]},
                        config={"configurable": {"thread_id": self.thread_id}},
                        context=self.context,
                        stream_mode=["tasks", "values"],
                    ):
                        if mode == "tasks":
                            self.trace.task(chunk)
                        else:
                            result = chunk
            except Exception:
                self.trace.fail_running()
                raise
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
            event_id = self.trace.start("text_to_speech", kind="speech")
            try:
                data = self.speech.synthesize(self.reply["text"])
                path = self.directory / f"reply-{uuid4().hex}.mp3"
                path.write_bytes(data)
                self.audio = str(path)
            except (SpeechError, OSError) as error:
                self.trace.finish(event_id, failed=True)
                self.warning = (
                    str(error) if isinstance(error, SpeechError) else "No se pudo guardar el audio."
                ) + " El turno ya está guardado; usa Reintentar audio."
            except Exception:
                self.trace.finish(event_id, failed=True)
                raise
            else:
                self.trace.finish(event_id)
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
