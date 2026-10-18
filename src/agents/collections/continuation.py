"""Contextual continuation intent; the backend alone authorizes state transitions."""

import json
from typing import Literal

from anthropic import APIError
from pydantic import BaseModel, ConfigDict

from agents.collections.conversation import normalize_text, requests_end
from agents.collections.onboarding_wording import redact_dialogue
from agents.collections.prompts import render_prompt


class ContinuationIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Literal["resume", "stop", "decline", "unclear"] = "unclear"


PROMPT = render_prompt("continuation")


def interpret_continuation(state, session, ctx):
    text = state["text"]
    if requests_end(text):
        return "stop", "deterministic"
    if ctx.provider == "mock":
        # Limited offline fixture; the real provider always interprets language in context.
        normalized = normalize_text(text)
        if normalized in {
            "continuemos",
            "retomemos",
            "quiero continuar",
            "quiero retomar",
            "sigue",
            "dale",
            "si",
            "si, adelante",
        }:
            return "resume", "mock"
        if normalized in {"no", "ahora no", "no gracias"}:
            return "decline", "mock"
        return "unclear", "mock"
    if ctx.provider != "anthropic":
        raise ValueError(f"Unsupported provider: {ctx.provider}")
    dialogue = []
    for message in state.get("messages", [])[-7:-1]:
        if message.type in {"human", "ai"} and isinstance(message.content, str):
            dialogue.append({"role": message.type, "text": redact_dialogue(message.content)})
    if not dialogue:
        dialogue = [
            {"user": redact_dialogue(turn["user"]), "assistant": redact_dialogue(turn["assistant"])}
            for turn in session.get("history", [])[-3:]
        ]
    try:
        from langchain_anthropic import ChatAnthropic

        from agents.collections.streaming import internal_model_call

        client = ChatAnthropic(
            model=ctx.model,
            temperature=0,
            max_tokens=128,
            timeout=15,
            max_retries=0,
            tags=["langsmith:nostream"],
        )
        result = internal_model_call(
            client.with_structured_output(ContinuationIntent),
            [
                ("system", PROMPT),
                (
                    "human",
                    json.dumps(
                        {
                            "current_message": redact_dialogue(text, max_length=None),
                            "recent_dialogue": dialogue,
                        },
                        ensure_ascii=False,
                    ),
                ),
            ],
        )
        return ContinuationIntent.model_validate(result).intent, "classified"
    except (APIError, ValueError, TimeoutError, ConnectionError):
        return "unclear", "model_error"
