"""Voice turns around the existing graph, gated by current-offer playback completion."""

import hashlib
import importlib
import importlib.abc
import importlib.machinery
import json
import sys
from copy import deepcopy
from pathlib import Path
from threading import RLock
from time import perf_counter
from uuid import uuid4

from langchain_core.messages import HumanMessage

from agents.collections.agent import build_graph
from collections_lab.channels.elevenlabs import SpeechError


class VoiceInputError(ValueError):
    """An intentionally user-facing validation failure."""


def graph_topology(graph):
    """Lay out any graph in non-overlapping layers; cycles remain valid edges."""
    topology = graph.get_graph()
    nodes = list(topology.nodes)
    edges = [
        {"source": edge.source, "target": edge.target, "conditional": edge.conditional}
        for edge in topology.edges
    ]
    outgoing = {node: [] for node in nodes}
    for edge in edges:
        outgoing[edge["source"]].append(edge["target"])
    # Ignore DFS back edges only while assigning layers, never in the displayed topology.
    visited, active, back_edges, order = set(), set(), set(), []

    def visit(node):
        if node in visited:
            return
        visited.add(node)
        active.add(node)
        for target in sorted(outgoing[node]):
            if target in active:
                back_edges.add((node, target))
            else:
                visit(target)
        active.remove(node)
        order.append(node)

    for node in sorted(nodes, key=lambda name: (name != "__start__", name)):
        visit(node)
    ranks = dict.fromkeys(nodes, 0)
    for source in reversed(order):
        for target in outgoing[source]:
            if (source, target) not in back_edges:
                ranks[target] = max(ranks[target], ranks[source] + 1)
    layers = {}
    for node in nodes:
        layers.setdefault(ranks[node], []).append(node)
    width = max(520, max((len(layer) for layer in layers.values()), default=1) * 180 + 40)
    positions = {}
    for rank, layer in layers.items():
        for column, node in enumerate(sorted(layer)):
            positions[node] = [width / 2 + (column - (len(layer) - 1) / 2) * 180, 40 + rank * 90]
    return {
        "nodes": nodes,
        "edges": edges,
        "positions": positions,
        "width": width,
        "height": max(160, 80 + max(ranks.values(), default=0) * 90),
    }


class FreshGraphSource(importlib.machinery.SourceFileLoader):
    """Compile local source without reading/writing bytecode caches during reload."""

    def get_code(self, fullname):
        filename = self.get_filename(fullname)
        return compile(Path(filename).read_bytes(), filename, "exec", dont_inherit=True)


class GraphSourceFinder(importlib.abc.MetaPathFinder):
    def __init__(self, source_root):
        self.source_root = source_root

    def find_spec(self, fullname, path=None, target=None):
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if (
            spec
            and spec.origin
            and isinstance(spec.loader, importlib.machinery.SourceFileLoader)
            and Path(spec.origin).resolve().is_relative_to(self.source_root)
            and not fullname.startswith("collections_lab")
        ):
            spec.loader = FreshGraphSource(fullname, spec.origin)
            return spec
        return None


class GraphRegistry:
    """Trusted local LangGraph manifest; refresh only with no leased calls."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self._lock = RLock()
        self._graphs = {}
        self._version = None
        self._leases = 0
        self._error = False
        self._pending = False
        self._attempted = None

    def _fingerprint(self):
        paths = [self.root / "langgraph.json"]
        paths.extend(
            sorted(
                path
                for path in (self.root / "src").rglob("*")
                if path.is_file() and path.suffix in {".py", ".jinja", ".jinja2"}
            )
        )
        digest = hashlib.sha256()
        for path in paths:
            digest.update(str(path.relative_to(self.root)).encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()[:12]

    def _refresh(self):
        try:
            version = self._fingerprint()
            self._pending = version != self._version
            if not self._pending:
                self._error = False
                return
            if self._leases or (self._error and version == self._attempted):
                return
            self._attempted = version
            manifest = json.loads((self.root / "langgraph.json").read_text())["graphs"]
            if not isinstance(manifest, dict) or not manifest:
                raise VoiceInputError("Empty graph registry")
            src = self.root / "src"
            # Drop only this repository's agent modules, never this running server/adapter.
            removed = {
                name: module
                for name, module in list(sys.modules.items())
                if getattr(module, "__file__", None)
                and Path(module.__file__).resolve().is_relative_to(src)
                and not name.startswith("collections_lab")
            }
            for name in removed:
                del sys.modules[name]
            importlib.invalidate_caches()
            finder = GraphSourceFinder(src)
            sys.meta_path.insert(0, finder)
            try:
                graphs = {}
                for graph_id, reference in manifest.items():
                    if not isinstance(reference, str) or ":" not in reference:
                        raise VoiceInputError("Only compiled graph references are supported")
                    module_ref, attribute = reference.rsplit(":", 1)
                    if module_ref.endswith(".py"):
                        path = (self.root / module_ref).resolve()
                        module_name = ".".join(path.relative_to(src).with_suffix("").parts)
                    else:
                        module_name = module_ref
                    local_path = src.joinpath(*module_name.split("."))
                    if (
                        not local_path.with_suffix(".py").exists()
                        and not (local_path / "__init__.py").exists()
                    ):
                        raise VoiceInputError("Graph module must belong to this repository")
                    # Only trusted, explicitly registered local graph entry points are imported.
                    module = importlib.import_module(module_name)
                    if not Path(module.__file__).resolve().is_relative_to(src):
                        raise VoiceInputError(
                            "Graph entry must belong to this repository's src directory"
                        )
                    graph = getattr(module, attribute)
                    if not hasattr(graph, "stream") or not hasattr(graph, "get_graph"):
                        raise VoiceInputError("Reference is not a compiled graph")
                    graphs[graph_id] = {"graph": graph, "topology": graph_topology(graph)}
            except Exception:
                for name, module in list(sys.modules.items()):
                    if (
                        getattr(module, "__file__", None)
                        and Path(module.__file__).resolve().is_relative_to(src)
                        and name not in removed
                        and not name.startswith("collections_lab")
                    ):
                        del sys.modules[name]
                sys.modules.update(removed)
                for name, module in removed.items():
                    parent, _, child = name.rpartition(".")
                    if parent in sys.modules:
                        setattr(sys.modules[parent], child, module)
                raise
            finally:
                sys.meta_path.remove(finder)
            self._graphs, self._version = graphs, version
            self._error = self._pending = False
        except Exception:  # noqa: BLE001 - preserve the last valid graph catalog
            self._error = True

    def catalog(self):
        with self._lock:
            self._refresh()
            return {
                "version": self._version,
                "pending_update": self._pending,
                "error": self._error,
                "graphs": [
                    {"id": name, "topology": item["topology"]}
                    for name, item in self._graphs.items()
                ],
            }

    def acquire(self, graph_id="collections_agent"):
        with self._lock:
            self._refresh()
            if graph_id not in self._graphs:
                raise VoiceInputError(
                    "El grafo de la llamada no está disponible en langgraph.json."
                )
            self._leases += 1
            return self._graphs[graph_id]["graph"], graph_id, self._version

    def release(self):
        with self._lock:
            self._leases = max(0, self._leases - 1)


class ExecutionTrace:
    """Bounded, sanitized execution evidence readable without the business-turn lock."""

    def __init__(self, graph, session_id):
        self.topology = graph_topology(graph)
        self.graph_id = "collections_agent"
        self.version = "loaded"
        self.graph_complete = False
        self.first_node_step = None
        self.session_id = session_id
        self.turn = 0
        self.events = []
        self.revision = 0
        self._lock = RLock()

    def begin(self):
        with self._lock:
            self.turn += 1
            self.events = []
            self.graph_complete = False
            self.first_node_step = None
            self.revision += 1

    def start(self, name, *, event_id=None, kind="node", step=None):
        with self._lock:
            event_id = event_id or uuid4().hex
            if kind == "node" and step is not None and self.first_node_step is None:
                self.first_node_step = step
            self.events.append(
                {
                    "id": event_id,
                    "name": name,
                    "kind": kind,
                    "step": step,
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

    def task(self, event, step=None):
        if event["name"] not in self.topology["nodes"]:
            return
        if "result" not in event and "error" not in event:
            self.start(event["name"], event_id=event["id"], step=step)
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

    def complete(self):
        with self._lock:
            self.graph_complete = True
            self.revision += 1

    def transitions(self):
        nodes = [event for event in self.events if event["kind"] == "node"]
        paths = set()
        for target in nodes:
            if target["step"] is None:
                continue
            incoming = [edge for edge in self.topology["edges"] if edge["target"] == target["name"]]
            candidates = [
                edge
                for edge in incoming
                if any(
                    source["name"] == edge["source"]
                    and source["status"] == "complete"
                    and source["step"] == target["step"] - 1
                    for source in nodes
                )
            ]
            for edge in candidates:
                # Ambiguous conditional fan-in is not asserted as an executed transition.
                if not edge["conditional"] or len(candidates) == 1:
                    paths.add((edge["source"], edge["target"]))
            if target["step"] == self.first_node_step:
                for edge in incoming:
                    if edge["source"] == "__start__":
                        paths.add((edge["source"], edge["target"]))
        if self.graph_complete:
            for edge in self.topology["edges"]:
                if edge["target"] == "__end__" and any(
                    event["name"] == edge["source"] and event["status"] == "complete"
                    for event in nodes
                ):
                    paths.add((edge["source"], edge["target"]))
        return [list(pair) for pair in sorted(paths)]

    def fail_running(self):
        with self._lock:
            self.graph_complete = False
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
                "graph_id": self.graph_id,
                "graph_version": self.version,
                "graph_complete": self.graph_complete,
                "transitions": self.transitions(),
                "turn": self.turn,
                "revision": self.revision,
                "topology": self.topology,
                "events": events,
            }


class VoiceSession:
    def __init__(self, context, speech, directory: Path, *, graph_registry=None):
        self.context = context
        self.speech = speech
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.thread_id = uuid4().hex
        self.graph_registry = graph_registry
        self._released = False
        self.failed = False
        if graph_registry:
            self.graph, graph_id, version = graph_registry.acquire()
            try:
                runtime = importlib.import_module("agents.collections.runtime")
                memory = importlib.import_module("agents.collections.memory")
                self.context = runtime.AppContext(
                    memory.SQLiteRepository(context.repository.path),
                    context.now,
                    context.provider,
                    context.model,
                    stream_responses=False,
                )
            except Exception:
                graph_registry.release()
                raise
        else:
            self.graph, graph_id, version = build_graph(studio=True), "collections_agent", "loaded"
        self.trace = ExecutionTrace(self.graph, self.thread_id)
        self.trace.graph_id, self.trace.version = graph_id, version
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

    def release_graph(self):
        if self.graph_registry and not self._released:
            self._released = True
            self.graph_registry.release()

    def end_call(self):
        """Close the local transport without interpreting synthetic customer speech."""
        with self._lock:
            if not self.reply or not self.reply["finished"]:
                self.reply = {"finished": True, "stage": "closed", "text": "Llamada finalizada."}
            self.audio = self.played_audio = None
            self.warning = ""
            self.release_graph()
            return self.reply

    def requires_offer_playback(self):
        return bool(
            self.reply
            and not self.reply["finished"]
            and (self.reply["stage"] == "awaiting_confirmation" or self.reply.get("offer_id"))
        )

    def check_turn(self):
        if self.failed:
            raise VoiceInputError("El turno falló. Finaliza esta llamada antes de continuar.")
        if self.reply and self.reply["finished"]:
            raise VoiceInputError("Esta sesión terminó. Inicia una nueva conversación.")
        if self.requires_offer_playback() and (not self.audio or self.played_audio != self.audio):
            raise VoiceInputError(
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
                raise VoiceInputError("Escribe o graba un mensaje de entre 1 y 4000 caracteres.")
            self.check_turn()
            if new_turn:
                self.trace.begin()
            started = perf_counter()
            try:
                with self.context.repository.connection():
                    result = None
                    for mode, chunk in self.graph.stream(
                        {"messages": [HumanMessage(content=text, id=uuid4().hex)]},
                        config={"configurable": {"thread_id": self.thread_id}},
                        context=self.context,
                        stream_mode=["debug", "values"],
                    ):
                        if mode == "debug":
                            if chunk["type"] in {"task", "task_result"}:
                                self.trace.task(chunk["payload"], chunk["step"])
                        elif mode == "values":
                            result = chunk
                    reply = result.get("reply") if isinstance(result, dict) else None
                    if (
                        not isinstance(reply, dict)
                        or not isinstance(reply.get("text"), str)
                        or not isinstance(reply.get("stage"), str)
                        or not isinstance(reply.get("finished"), bool)
                    ):
                        raise TypeError("Graph returned an invalid voice reply")
                    if reply["finished"] and reply.get("continuation_available") is True:
                        # Call lifetime is not business lifetime: keep listening for the
                        # customer's own continuation request. Nothing is reopened here.
                        reply = {**reply, "finished": False}
                    self.trace.complete()
                self.reply = reply
                self.latencies["agent_seconds"] = round(perf_counter() - started, 3)
                self.history.extend(
                    [
                        {"role": "user", "content": text},
                        {"role": "assistant", "content": self.reply["text"]},
                    ]
                )
            except Exception:
                self.failed = True
                self.trace.fail_running()
                raise
            # Persist the turn before synthesis. An audio retry never replays the graph.
            if speak:
                self.retry_audio()
            else:
                self.audio = self.played_audio = None
                self.warning = ""
            return self.reply

    def retry_audio(self):
        with self._lock:
            if self.failed:
                raise VoiceInputError("El turno falló. Finaliza esta llamada antes de continuar.")
            if self.reply is None:
                raise VoiceInputError("Inicia una sesión primero.")
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
