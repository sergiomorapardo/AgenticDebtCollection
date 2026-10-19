"""Contextual pre-verification wording with a bounded draft/review loop."""

import re

from anthropic import APIError
from pydantic import BaseModel, ConfigDict

from agents.collections.customer_text import contains_internal_marker, sanitize_customer_text
from agents.collections.prompts import render_prompt
from agents.collections.wording import ReplyDraft, _request


class OnboardingReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    addresses_current_message: bool
    respects_scope_and_pause: bool
    no_unsupported_claims: bool
    avoids_repetition: bool


DRAFT_PROMPT = render_prompt("onboarding_draft")

REVIEW_PROMPT = render_prompt("onboarding_review")


def redact_dialogue(text, *, max_length=600):
    text = sanitize_customer_text(text)
    text = re.sub(r"https?://\S+|www\.\S+|[\w.+-]+@[\w.-]+", "un dato de contacto", text)
    redacted = re.sub(r"\d+", "una cifra", text)
    return redacted if max_length is None else redacted[:max_length]


def contextual_reply(
    ctx, text, history, reference, paused, intent=None, *, stage=None, next_step=None
):
    system = render_prompt("onboarding_draft", stage=stage)
    payload = {
        "current_message": redact_dialogue(text, max_length=None),
        "next_step": next_step if not paused else None,
        "recent_dialogue": [
            {key: redact_dialogue(value) for key, value in turn.items()} for turn in history[-4:]
        ],
        "reference": redact_dialogue(reference),
        "paused": paused,
        "intent": intent,
        "first_response": not history,
    }
    for attempt in range(1, 3):
        try:
            candidate = _request(ctx, ReplyDraft, system, payload).text
            prohibited = re.search(
                r"\d|https?://|www\.|\b(embarg\w*|demanda\w*|carcel|cárcel|datacredito|datacrédito)\b",
                candidate,
                re.IGNORECASE,
            )
            repeated = any(candidate == turn["assistant"] for turn in history[-2:])
            if not prohibited and not repeated and not contains_internal_marker(candidate):
                review = _request(
                    ctx, OnboardingReview, REVIEW_PROMPT, {**payload, "candidate": candidate}
                )
                if all(review.model_dump().values()):
                    return candidate, "validated", attempt
            payload["previous_draft_rejected"] = True
        except (APIError, ValueError, TimeoutError, ConnectionError):
            return reference, "model_error", attempt
    return reference, "rejected", 2
