"""Interpret pre-verification conversation without account access or financial actions."""

import json
import re
from typing import Literal

from anthropic import APIError
from pydantic import BaseModel, ConfigDict

from agents.collections.conversation import normalize_text, social_intent
from agents.collections.onboarding_wording import contextual_reply, redact_dialogue
from agents.collections.prompts import render_prompt


class OnboardingIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Literal[
        "greeting", "decline", "willing", "complaint", "scope", "identity_help", "unclear"
    ] = "unclear"


def classify_onboarding(text, ctx, history=None):
    normalized = normalize_text(text)
    if getattr(ctx, "stream_responses", False) and normalized in {
        "hola",
        "buenas",
        "buenos dias",
        "buenas tardes",
        "que hay",
    }:
        return "greeting"
    if ctx.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        client = ChatAnthropic(
            model=ctx.model,
            temperature=0,
            max_tokens=128,
            timeout=15,
            max_retries=0,
            tags=["langsmith:nostream"],
        )
        from agents.collections.streaming import internal_model_call

        result = internal_model_call(
            client.with_structured_output(OnboardingIntent),
            [
                ("system", render_prompt("onboarding_classify")),
                (
                    "human",
                    json.dumps(
                        {
                            "current_message": redact_dialogue(text),
                            "recent_dialogue": (history or [])[-4:],
                        },
                        ensure_ascii=False,
                    ),
                ),
            ],
        )
        return OnboardingIntent.model_validate(result).intent
    if ctx.provider != "mock":
        raise ValueError(f"Unsupported provider: {ctx.provider}")
    if re.search(r"\bno (?:quiero|voy a|deseo)\b", normalized):
        return "decline"
    if re.search(
        r"\b(quiero pagar|voy a [op]?pagar|si quiero|continuemos|retomemos|quiero continuar)\b",
        normalized,
    ):
        return "willing"
    if re.search(r"\b(como|cual|cuales|ejemplo)\b", normalized) and re.search(
        r"\b(cedulas?|identificacion|codigo|verificar)\b", normalized
    ):
        return "identity_help"
    boundary = social_intent(text)
    if boundary:
        return "complaint" if boundary == "complaint" else "scope"
    if normalized in {"hola", "que hay", "buenas", "buenos dias", "buenas tardes"}:
        return "greeting"
    return "unclear"


def onboarding_reply(text, binding, ctx):
    """Return an authorized reply and updated conversational memory, never business state."""
    binding = dict(binding)
    selected = bool(binding["account_id"])
    stage = "awaiting_identity" if selected else "awaiting_customer"
    step = (
        "Para continuar, compárteme el código de verificación."
        if selected
        else "Para consultar tu cuenta, ¿me compartes tu número de identificación?"
    )
    history = binding.get("onboarding_history", [])[-4:]
    status = "classified"
    try:
        intent = classify_onboarding(text, ctx, history)
    except (APIError, ValueError, TimeoutError, ConnectionError):
        intent, status = "unclear", "model_error"
    previous = binding.get("onboarding_intent")
    if intent == "decline":
        binding["onboarding_paused"] = True
        response = (
            "Entendido. No insistiré ni te pediré datos ahora. Podemos dejarlo aquí; si cambias de idea, dime que quieres continuar."
            if previous != "decline"
            else "Te escucho. Dejamos la conversación en pausa, sin consultar tu cuenta ni registrar pagos."
        )
    elif intent == "willing":
        binding["onboarding_paused"] = False
        response = "De acuerdo, podemos revisar cómo seguir. Antes de hablar de una cuenta debo verificar la identidad. "
    elif intent == "complaint":
        response = "Lamento la frustración. Todavía no he consultado tu cuenta. Puedes decirme qué necesitas aclarar o pedir que terminemos."
    elif intent == "identity_help":
        response = (
            "Claro, puedo explicarte cómo verificar tu identidad antes de consultar la cuenta."
        )
    elif intent == "scope":
        response = "Soy un asistente virtual de atención de cartera. La verificación de identidad protege el acceso a tu cuenta. Puedo aclarar tus dudas y tú decides si continuar."
    elif binding.get("onboarding_paused"):
        response = "Seguimos en pausa. Puedo aclarar tus dudas; no necesitas dar datos si no quieres continuar."
    elif not previous and intent == "greeting":
        response = "Hola, soy el asistente virtual de atención de cartera."
    else:
        response = "Puedo aclarar tus dudas sobre la atención de cartera sin consultar ninguna cuenta. ¿Qué te gustaría saber?"
    if status == "model_error":
        response = "No pude interpretar tu mensaje por un error del servicio. No consulté tu cuenta ni registré cambios; puedes reintentar o escribir terminar."
    wording_status, attempts = "deterministic", 0
    streamed = {}
    if ctx.provider == "anthropic" and status != "model_error":
        if getattr(ctx, "stream_responses", False):
            # Identity instructions and first greeting are already authorized, short text.
            if intent not in {"willing", "identity_help"} and not (
                not previous and intent == "greeting"
            ):
                from agents.collections.onboarding_wording import DRAFT_PROMPT
                from agents.collections.streaming import stream_reply

                streamed = stream_reply(
                    ctx,
                    DRAFT_PROMPT,
                    {
                        "current_message": redact_dialogue(text),
                        "recent_dialogue": history,
                        "reference": redact_dialogue(response),
                        "intent": intent,
                        "paused": binding.get("onboarding_paused", False),
                        "first_response": not history,
                    },
                )
                response, wording_status, attempts = streamed["text"], streamed["wording_status"], 1
        else:
            response, wording_status, attempts = contextual_reply(
                ctx, text, history, response, binding.get("onboarding_paused", False), intent=intent
            )
    if status != "model_error" and (
        intent in {"willing", "identity_help"} or (not previous and intent == "greeting")
    ):
        response += " " + step
    binding["onboarding_intent"] = intent
    binding["onboarding_history"] = (
        history + [{"user": redact_dialogue(text), "assistant": redact_dialogue(response)}]
    )[-4:]
    return binding, {
        "text": response,
        "stage": stage,
        "finished": False,
        "interpretation_status": status,
        "wording_status": wording_status,
        "wording_attempts": attempts,
        **streamed,
    }
