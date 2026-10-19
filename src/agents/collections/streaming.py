"""Stream only customer-facing prose; business mutations remain outside generation."""

import json
from time import perf_counter
from typing import Any

from anthropic import APIError
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.runnables.config import ensure_config, set_config_context
from pydantic import Field

from agents.collections.customer_text import CustomerTextFilter

PLAIN_TEXT = """Return only the customer-facing Spanish reply as plain text, never JSON,
reasoning, tool calls or a schema. Follow the authorized reference and scope. You cannot
verify identity, save payments, accept agreements or perform actions. Never claim otherwise.
Use at most two brief sentences. The response is shown immediately while you write it."""


class CustomerStreamModel(BaseChatModel):
    """Emit only sanitized LangChain callbacks; the underlying provider is internal."""

    source: Any = Field(exclude=True)
    outcome: str = "streamed"

    @property
    def _llm_type(self):
        return "safe-customer-stream"

    def _generate(self, *args, **kwargs):
        raise NotImplementedError("Customer replies must use streaming")

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        filtered = CustomerTextFilter()
        emitted = False
        config = ensure_config()
        config["callbacks"] = []
        config["tags"] = ["langsmith:nostream"]
        with set_config_context(config) as context:
            iterator = context.run(self.source.stream, messages, config=config, stop=stop)
            try:
                while True:
                    try:
                        chunk = context.run(next, iterator)
                    except StopIteration:
                        break
                    content = chunk.content
                    raw = (
                        content
                        if isinstance(content, str)
                        else "".join(
                            block.get("text", "")
                            for block in content
                            if isinstance(block, dict) and block.get("type") == "text"
                        )
                    )
                    visible = filtered.feed(raw)
                    if visible:
                        emitted = emitted or bool(visible.strip())
                        yield ChatGenerationChunk(message=AIMessageChunk(content=visible))
                tail = filtered.finish()
                if tail:
                    emitted = emitted or bool(tail.strip())
                    yield ChatGenerationChunk(message=AIMessageChunk(content=tail))
                if not emitted:
                    raise ValueError("Empty customer response")
            except (APIError, ValueError, TimeoutError, ConnectionError):
                tail = filtered.finish()
                if tail:
                    emitted = emitted or bool(tail.strip())
                    yield ChatGenerationChunk(message=AIMessageChunk(content=tail))
                self.outcome = "interrupted" if emitted else "model_error"
                notice = (
                    "\nLa respuesta se interrumpió; puedes reintentar."
                    if emitted
                    else "No pude generar la respuesta. Puedes reintentar; no registré ningún acuerdo."
                )
                yield ChatGenerationChunk(message=AIMessageChunk(content=notice))
            finally:
                close = getattr(iterator, "close", None)
                if close is not None:
                    context.run(close)


def stream_reply(ctx, system, payload):
    from langchain_anthropic import ChatAnthropic

    started = perf_counter()
    first_token = None
    text = ""
    message_id = None
    provider = ChatAnthropic(
        model=ctx.model,
        temperature=0,
        max_tokens=240,
        timeout=20,
        max_retries=0,
        callbacks=[],
        tags=["langsmith:nostream"],
    )
    client = CustomerStreamModel(source=provider, tags=["customer_reply"])
    for chunk in client.stream(
        [
            ("system", system + "\n\n" + PLAIN_TEXT),
            ("human", json.dumps(payload, ensure_ascii=False)),
        ]
    ):
        message_id = chunk.id or message_id
        if chunk.content:
            if first_token is None:
                first_token = perf_counter()
            text += chunk.content
    return {
        "text": text,
        "message_id": message_id,
        "wording_status": client.outcome,
        "first_token_ms": round((first_token - started) * 1000) if first_token else None,
        "generation_ms": round((perf_counter() - started) * 1000),
    }


def internal_model_call(runnable, messages):
    """Keep classifier/reviewer events out of the Agent Server customer message channel."""
    config = ensure_config()
    # Agent Server 0.15.4 forwards raw events even with langsmith:nostream tags.
    # Detach graph streaming callbacks for internal calls; never log their content.
    config["callbacks"] = []
    with set_config_context(config) as context:
        return context.run(runnable.invoke, messages)
