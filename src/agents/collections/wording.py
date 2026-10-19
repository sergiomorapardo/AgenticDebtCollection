"""Bounded conversational wording; financial terms and business effects stay in code."""

import json
import re

from anthropic import APIError
from pydantic import BaseModel, ConfigDict, Field

from agents.collections.customer_text import contains_internal_marker
from agents.collections.prompts import render_prompt
from agents.collections.runtime import resolve_context


class ReplyDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=600)


class ReplyReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preserves_next_step: bool
    no_new_claims: bool
    respectful: bool


DRAFT_PROMPT = render_prompt("reply_draft")

REVIEW_PROMPT = render_prompt("reply_review")


def _request(ctx, schema, system, payload):
    from langchain_anthropic import ChatAnthropic

    client = ChatAnthropic(
        model=ctx.model,
        temperature=0,
        max_tokens=400,
        timeout=20,
        max_retries=0,
        tags=["langsmith:nostream"],
    )
    from agents.collections.streaming import internal_model_call

    result = internal_model_call(
        client.with_structured_output(schema),
        [("system", system), ("human", json.dumps(payload, ensure_ascii=False))],
    )
    return schema.model_validate(result)


def prepare_reply(state, runtime):
    """Reset per-turn attempt state, including on persisted Studio checkpoints."""
    ctx = resolve_context(runtime)
    reference = state.get("response_text", "")
    enabled = (
        ctx.provider == "anthropic"
        and not state.get("replay")
        and not state.get("commit_requested")
        and not state.get("draft_offer")
        and state.get("interpretation")
        and state["session"].get("identity_verified")
        and state["session"]["stage"] not in {"agreed", "closed", "handoff"}
        and "?" in reference
        and not re.search(r"\d|COP|AAAA|servicio", reference)
    )
    return {
        "streamed_reply": None,
        "wording_reference": reference,
        "wording_draft": "",
        "wording_attempts": 0,
        "wording_status": "pending" if enabled else "deterministic",
        "wording_route": "draft_reply" if enabled else "response",
    }


def _dialogue_payload(state):
    from agents.collections.onboarding_wording import redact_dialogue

    history = [
        {"user": redact_dialogue(turn["user"]), "assistant": redact_dialogue(turn["assistant"])}
        for turn in state["session"].get("history", [])[-4:]
    ]
    return {
        "reference": state["wording_reference"],
        "current_message": redact_dialogue(state.get("text", ""), max_length=None),
        "recent_dialogue": history,
        "intent": state.get("interpretation", {}).get("intent"),
    }


def draft_reply(state, runtime):
    ctx = resolve_context(runtime)
    system = render_prompt("reply_draft", stage=state["session"]["stage"])
    payload = _dialogue_payload(state)
    if ctx.stream_responses:
        from agents.collections.streaming import stream_reply

        streamed = stream_reply(ctx, system, payload)
        return {
            "streamed_reply": streamed,
            "response_text": streamed["text"],
            "wording_status": streamed["wording_status"],
            "wording_attempts": 1,
        }
    payload["previous_draft_rejected"] = state["wording_attempts"] > 0
    result = {"wording_attempts": state["wording_attempts"] + 1}
    try:
        candidate = _request(ctx, ReplyDraft, system, payload)
    except (APIError, ValueError, TimeoutError, ConnectionError):
        return {**result, "wording_draft": "", "wording_status": "model_error"}
    return {**result, "wording_draft": candidate.text, "wording_status": "pending"}


def review_reply(state, runtime):
    """Reject altered meaning; permit one regeneration before deterministic fallback."""
    candidate = state["wording_draft"]
    accepted = False
    status = state["wording_status"]
    # The wording path is for non-financial questions only. Never pass new numeric
    # terms, links, or obvious threats to the customer, even if the reviewer accepts.
    prohibited = re.search(
        r"\d|https?://|www\.|\b(embarg\w*|demanda\w*|carcel|cárcel|datacrédito|datacredito|descuento\w*|condon\w*)\b",
        candidate,
        re.IGNORECASE,
    )
    if candidate and not prohibited and not contains_internal_marker(candidate):
        try:
            result = _request(
                resolve_context(runtime),
                ReplyReview,
                REVIEW_PROMPT,
                {**_dialogue_payload(state), "candidate": candidate},
            )
            accepted = all(result.model_dump().values())
        except (APIError, ValueError, TimeoutError, ConnectionError):
            status = "model_error"
    if accepted:
        return {
            "response_text": candidate,
            "wording_status": "validated",
            "wording_route": "response",
        }
    retry = status != "model_error" and state["wording_attempts"] < 2
    return {
        "response_text": state["wording_reference"],
        "wording_status": status if status == "model_error" else "rejected",
        "wording_route": "draft_reply" if retry else "response",
    }


def route_wording(state):
    return state["wording_route"]
