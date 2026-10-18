"""Stream only customer-facing prose; business mutations remain outside generation."""

import json
from time import perf_counter

from anthropic import APIError
from langchain_core.runnables.config import ensure_config, set_config_context

PLAIN_TEXT = """Return only the customer-facing Spanish reply as plain text, never JSON,
reasoning, tool calls or a schema. Follow the authorized reference and scope. You cannot
verify identity, save payments, accept agreements or perform actions. Never claim otherwise.
Use at most two brief sentences. The response is shown immediately while you write it."""


def stream_reply(ctx, system, payload):
    from langchain_anthropic import ChatAnthropic

    started = perf_counter()
    first_token = None
    text = ""
    message_id = None
    status = "streamed"
    client = ChatAnthropic(
        model=ctx.model,
        temperature=0,
        max_tokens=240,
        timeout=20,
        max_retries=0,
        tags=["customer_reply"],
    )
    try:
        for chunk in client.stream(
            [
                ("system", system + "\n\n" + PLAIN_TEXT),
                ("human", json.dumps(payload, ensure_ascii=False)),
            ]
        ):
            message_id = chunk.id or message_id
            content = chunk.content
            delta = (
                content
                if isinstance(content, str)
                else "".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            )
            if delta:
                if first_token is None:
                    first_token = perf_counter()
                text += delta
        if not text.strip():
            raise ValueError("Empty customer response")
    except (APIError, ValueError, TimeoutError, ConnectionError):
        status = "interrupted" if text else "model_error"
        text += (
            "\nLa respuesta se interrumpió; puedes reintentar."
            if text
            else "No pude generar la respuesta. Puedes reintentar; no registré ningún acuerdo."
        )
    return {
        "text": text,
        "message_id": message_id,
        "wording_status": status,
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
