"""Real LangGraph message-stream integration, replay identity and business isolation."""

import sys
from dataclasses import replace
from datetime import datetime
from threading import Event
from types import SimpleNamespace
from typing import ClassVar

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGenerationChunk
from langgraph.checkpoint.memory import InMemorySaver

from agents.collections.agent import build_graph
from agents.collections.chat_memory import ChatMemory
from agents.collections.memory import SQLiteRepository
from agents.collections.runtime import AppContext


class StreamingModel(BaseChatModel):
    consumed: ClassVar[Event] = Event()
    calls: ClassVar[int] = 0
    interrupt: ClassVar[bool] = False

    @property
    def _llm_type(self):
        return "streaming-test"

    def _generate(self, *args, **kwargs):
        raise AssertionError("Must use real streaming")

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        type(self).calls += 1
        yield ChatGenerationChunk(message=AIMessageChunk(content="Soy un asistente "))
        assert self.consumed.wait(5), "First token must reach consumer before generation ends"
        if self.interrupt:
            raise TimeoutError("synthetic disconnect")
        yield ChatGenerationChunk(message=AIMessageChunk(content="virtual."))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from agents.collections import onboarding, semantic

    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setattr(onboarding, "classify_onboarding", lambda *args: "scope")
    understand = semantic.understand_turn

    def offline_understanding(text, **kwargs):
        return understand(text, **{**kwargs, "ctx": replace(kwargs["ctx"], provider="mock")})

    monkeypatch.setattr(semantic, "understand_turn", offline_understanding)
    monkeypatch.setitem(
        sys.modules, "langchain_anthropic", SimpleNamespace(ChatAnthropic=StreamingModel)
    )
    StreamingModel.consumed.clear()
    StreamingModel.calls = 0
    StreamingModel.interrupt = False
    ctx = AppContext(
        SQLiteRepository(tmp_path / "stream.db"),
        datetime.fromisoformat("2026-10-19T09:00:00-05:00"),
        provider="anthropic",
        stream_responses=True,
    )
    graph = build_graph(studio=True, checkpointer=InMemorySaver())
    return graph, ctx, {"configurable": {"thread_id": "stream"}}


@pytest.mark.parametrize("interrupted", [False, True])
def test_tokens_arrive_before_completion_and_replay_keeps_one_message(setup, interrupted):
    graph, ctx, config = setup
    StreamingModel.interrupt = interrupted
    request = {"messages": [HumanMessage(content="¿Eres un bot?", id="question")]}
    chunks = []
    final = None
    for mode, value in graph.stream(
        request, config=config, context=ctx, stream_mode=["messages", "values"]
    ):
        if mode == "messages":
            message, metadata = value
            if "customer_reply" in metadata.get("tags", []):
                chunks.append(message)
                StreamingModel.consumed.set()
        else:
            final = value
    assert chunks and chunks[0].content == "Soy un asistente "
    assert final["reply"]["wording_status"] == ("interrupted" if interrupted else "streamed")
    assert final["messages"][-1].id == chunks[0].id == final["reply"]["message_id"]
    assert final["reply"]["first_token_ms"] is not None
    assert (
        "interrumpió" in final["reply"]["text"]
        if interrupted
        else final["reply"]["text"] == "Soy un asistente virtual."
    )
    replay = graph.invoke(request, config=config, context=ctx)
    assert replay["reply"] == final["reply"]
    assert len(replay["messages"]) == 2
    assert StreamingModel.calls == 1
    assert len(ChatMemory(ctx.repository).binding("stream")["onboarding_history"]) == 1
    assert not ctx.repository.agreements()


def test_streaming_financial_reply_is_persisted_before_delivery(setup, monkeypatch):
    graph, ctx, config = setup
    for index, text in enumerate(
        ["1000000001", "verificar 1111", "quiero 3 cuotas desde mañana", "confirmo"]
    ):
        result = graph.invoke(
            {"messages": [HumanMessage(content=text, id=str(index))]}, config=config, context=ctx
        )
    assert result["reply"]["stage"] == "agreed"
    assert len(ctx.repository.agreements()) == 1
    assert StreamingModel.calls == 0
    assert result["reply"]["wording_status"] == "deterministic"


def test_verified_conversational_stream_skips_full_text_review(setup, monkeypatch):
    graph, ctx, config = setup
    from agents.collections import wording

    monkeypatch.setattr(
        wording, "_request", lambda *a: pytest.fail("No post-generation reviewer in streaming mode")
    )
    for index, text in enumerate(["1000000001", "verificar 1111"]):
        graph.invoke(
            {"messages": [HumanMessage(content=text, id=str(index))]}, config=config, context=ctx
        )
    for mode, value in graph.stream(
        {"messages": [HumanMessage(content="No tengo ingresos", id="hardship")]},
        config=config,
        context=ctx,
        stream_mode=["messages", "values"],
    ):
        if mode == "messages" and "customer_reply" in value[1].get("tags", []):
            StreamingModel.consumed.set()
        if mode == "values":
            final = value
    assert final["reply"]["wording_status"] == "streamed"
    assert final["messages"][-1].id == final["reply"]["message_id"]
    assert not ctx.repository.agreements()


def test_internal_model_callbacks_do_not_emit_customer_messages():
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langgraph.graph import END, START, StateGraph
    from typing_extensions import TypedDict

    from agents.collections.streaming import internal_model_call

    class InternalState(TypedDict):
        value: str

    def classify(state):
        result = internal_model_call(
            FakeListChatModel(responses=["private classification"]), [HumanMessage(content="hello")]
        )
        return {"value": result.content}

    graph = (
        StateGraph(InternalState)
        .add_node("classify", classify)
        .add_edge(START, "classify")
        .add_edge("classify", END)
        .compile()
    )
    assert list(graph.stream({"value": ""}, stream_mode="messages")) == []
