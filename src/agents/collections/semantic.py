"""One semantic interpretation per human turn; all authorization stays in the backend."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from anthropic import APIError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents.collections.prompts import PromptStage, render_prompt


class TurnUnderstanding(BaseModel):
    """Customer-declared meaning, never account identity or permission to act."""

    model_config = ConfigDict(extra="forbid")
    intent: Literal[
        "greeting",
        "decline",
        "continue",
        "identity",
        "verification_code",
        "history",
        "options",
        "scope",
        "promise",
        "negotiate",
        "accept_offer",
        "reported_payment",
        "dispute",
        "request_human",
        "end",
        "unclear",
        "refuse",
        "hardship",
        "no_capacity",
        "reject_offer",
        "counteroffer",
        "request_exception",
        "off_topic",
        "complaint",
        "clarify_process",
    ] = "unclear"
    amount_cop: int | None = Field(
        default=None,
        gt=0,
        strict=True,
        description="Explicit positive COP amount, or null. For no_capacity use null, never zero.",
    )
    amount_is_capacity: bool = False
    frequency: Literal["monthly", "one_off", "unspecified"] = "unspecified"
    payment_date: date | None = None
    installments: int | None = Field(default=None, gt=0, strict=True)
    acceptance: Literal["none", "conditional", "unconditional_current_offer"] = "none"
    identity_token: int | None = Field(
        default=None,
        ge=0,
        strict=True,
        description="Zero-based numeric_candidates INDEX for the identification number, not its digit count or value. <number_0> selects index 0.",
    )
    code_token: int | None = Field(
        default=None,
        ge=0,
        strict=True,
        description="Zero-based numeric_candidates INDEX for the verification code, not digit count or value. <number_0> selects index 0, not 4.",
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_zero_capacity(cls, value):
        # Zero is a representation of the model's explicit no-capacity classification,
        # never a payable amount. No other intent can supply a zero financial amount.
        if (
            isinstance(value, dict)
            and value.get("intent") == "no_capacity"
            and type(value.get("amount_cop")) is int
            and value["amount_cop"] == 0
        ):
            return {**value, "amount_cop": None}
        return value


@dataclass(frozen=True)
class TurnResult:
    turn: TurnUnderstanding
    status: Literal["classified", "mock", "model_error"]
    identity_candidate: str | None = None
    verification_candidate: str | None = None


def _redact_contacts(text: str) -> str:
    return re.sub(r"https?://\S+|www\.\S+|[\w.+-]+@[\w.-]+", "[redacted]", text)


def _text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            item["text"]
            for item in value
            if isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        )
    return ""


def _recent_dialogue(history) -> list[dict]:
    result = []
    for item in list(history)[-4:]:
        if isinstance(item, dict):
            values = {
                key: item[key] for key in ("user", "assistant", "role", "text") if key in item
            }
            if "content" in item and "text" not in values:
                values["text"] = item["content"]
        else:
            values = {"role": getattr(item, "type", ""), "text": getattr(item, "content", "")}
        result.append(
            {
                key: re.sub(r"\d+", "[number]", _redact_contacts(_text_content(value)))[:600]
                for key, value in values.items()
                if isinstance(value, (str, list))
            }
        )
    return result


def _backend_facts(context: dict | None) -> dict:
    """Select bounded useful facts, excluding account/session/customer identifiers."""
    context = context or {}
    result = {}
    if isinstance(context.get("latest_assistant_prompt"), str):
        result["latest_assistant_prompt"] = re.sub(
            r"\d+", "[number]", _redact_contacts(context["latest_assistant_prompt"])
        )[:600]
    for key in ("recoverable_misclosure", "identity_verified"):
        if type(context.get(key)) is bool:
            result[key] = context[key]
    for key in ("pending_offer", "draft_terms", "dialogue"):
        value = context.get(key)
        if not isinstance(value, dict):
            continue
        selected = {}
        for name in (
            "amount_cop",
            "monthly_capacity_cop",
            "total_cop",
            "balance_cop",
            "installments",
        ):
            if type(value.get(name)) is int and 0 <= value[name] <= 10**15:
                selected[name] = value[name]
        for name in ("payment_date", "awaiting"):
            if isinstance(value.get(name), str):
                selected[name] = _redact_contacts(value[name])[:80]
        if type(value.get("amount_is_capacity")) is bool:
            selected["amount_is_capacity"] = value["amount_is_capacity"]
        if key == "pending_offer" and isinstance(value.get("installments"), list):
            selected["installments"] = [
                {
                    "amount_cop": item.get("amount_cop"),
                    "due_date": str(item.get("due_date", ""))[:10],
                }
                for item in value["installments"][:12]
                if isinstance(item, dict) and type(item.get("amount_cop")) is int
            ]
        result[key] = selected
    return result


def _resolve(turn: TurnUnderstanding, tokens: list[str], stage: PromptStage, status) -> TurnResult:
    if turn.identity_token is not None and turn.code_token is not None:
        raise ValueError("A turn cannot select both identifier roles")
    identity = code = None
    if turn.identity_token is not None:
        if stage != "awaiting_customer" or turn.intent != "identity":
            raise ValueError("Identity selection does not match the backend stage")
        if turn.identity_token >= len(tokens):
            raise ValueError("Identity token is absent")
        identity = tokens[turn.identity_token]
        if not 1 <= len(identity) <= 32:
            raise ValueError("Identity token exceeds the candidate limit")
    if turn.code_token is not None:
        if stage != "awaiting_identity" or turn.intent != "verification_code":
            raise ValueError("Code selection does not match the backend stage")
        if turn.code_token >= len(tokens):
            raise ValueError("Code token is absent")
        code = tokens[turn.code_token]
        if len(code) != 4:
            raise ValueError("Verification candidates must contain four digits")
    if (turn.intent == "identity" and identity is None) or (
        turn.intent == "verification_code" and code is None
    ):
        raise ValueError("No current-message token selected")
    if turn.intent == "accept_offer" and (
        turn.acceptance != "unconditional_current_offer"
        or stage != "awaiting_confirmation"
        or turn.frequency != "unspecified"
        or turn.amount_is_capacity
        or any(
            value is not None for value in (turn.amount_cop, turn.payment_date, turn.installments)
        )
    ):
        turn = turn.model_copy(update={"intent": "unclear"})
    return TurnResult(turn, status, identity, code)


def _mock_turn(text, *, today, stage, ctx, tokens, context):
    """Limited offline fixtures only; production never uses these lexical shortcuts."""
    from agents.collections.conversation import normalize_text, requests_end
    from agents.collections.interpreter import parse_turn
    from agents.collections.onboarding import classify_onboarding

    if requests_end(text):
        return TurnUnderstanding(intent="end")
    normalized = normalize_text(text)
    identity_submission = re.fullmatch(r"[0-9]+", normalized) or re.search(
        r"\b(cedula|identificacion|documento|codigo|verificar)\b|"
        r"\bmi numero\b|\bya te (?:lo )?(?:pase|di|envie|comparti)\b|"
        r"^(?:es|era) [0-9]+$",
        normalized,
    )
    if stage in {"awaiting_customer", "awaiting_identity"}:
        if len(tokens) == 1 and identity_submission:
            if stage == "awaiting_customer":
                return TurnUnderstanding(intent="identity", identity_token=0)
            if len(tokens[0]) == 4:
                return TurnUnderstanding(intent="verification_code", code_token=0)
        intent = classify_onboarding(text, ctx)
        mapped = {"willing": "continue", "identity_help": "scope"}.get(intent, intent)
        return TurnUnderstanding(intent=mapped)
    if context.get("recoverable_misclosure") and normalize_text(text).strip(" .!?¡¿") in {
        "si",
        "dale",
        "sigue",
        "continuemos",
        "sigamos",
        "retomemos",
        "si, adelante",
    }:
        return TurnUnderstanding(intent="continue")
    awaiting = context.get("dialogue", {}).get("awaiting")
    if awaiting == "capacity" and normalized.strip(" .!¡") in {
        "nada",
        "cero",
        "0",
        "no puedo destinar nada",
        "no me queda nada",
        "nada, este mes no me queda ni para comer",
    }:
        return TurnUnderstanding(intent="no_capacity")
    if awaiting == "frequency":
        if normalized.strip(" .") in {"mensual", "al mes", "cada mes", "mensuales"}:
            return TurnUnderstanding(intent="negotiate", frequency="monthly")
        if normalized.strip(" .") in {
            "una vez",
            "un solo pago",
            "solo esta vez",
            "un unico abono",
            "un abono",
            "una sola vez",
            "unico",
        }:
            return TurnUnderstanding(intent="promise", frequency="one_off")
    parsed = parse_turn(text, today=today, provider="mock")
    fields = parsed.model_dump(exclude={"identity_code"})
    if fields["intent"] == "accept_offer":
        if stage == "awaiting_confirmation":
            fields["acceptance"] = "unconditional_current_offer"
        else:
            fields["intent"] = "continue"
    return TurnUnderstanding(**fields)


def understand_turn(
    text: str, *, ctx, today: date, stage: PromptStage, history=(), context=None
) -> TurnResult:
    """Interpret the entire current message once, with bounded prior context.

    Numeric identity candidates are replaced by references before pre-verification
    interpretation. The model selects a reference; only the backend resolves its exact
    source text. Links/emails and prior-context numbers are redacted in every stage.
    """
    system = render_prompt("semantic_turn", stage=stage)
    if ctx.provider not in {"mock", "anthropic"}:
        raise ValueError(f"Unsupported provider: {ctx.provider}")
    tokens = []
    current = _redact_contacts(text)
    if (
        stage in {"awaiting_customer", "awaiting_identity"}
        or (context or {}).get("identity_verified") is False
    ):

        def replace(match):
            tokens.append(match.group())
            return f"<number_{len(tokens) - 1}>"

        current = re.sub(r"[0-9]+", replace, current)
    payload = {
        "current_message": current,
        "numeric_candidates": [
            {"index": index, "digits": len(value)} for index, value in enumerate(tokens)
        ],
        "today": today.isoformat(),
        "recent_dialogue": _recent_dialogue(history),
        "backend_context": _backend_facts(context),
    }
    try:
        if ctx.provider == "mock":
            turn = _mock_turn(
                text, today=today, stage=stage, ctx=ctx, tokens=tokens, context=context or {}
            )
            return _resolve(turn, tokens, stage, "mock")
        from langchain_anthropic import ChatAnthropic

        from agents.collections.streaming import internal_model_call

        client = ChatAnthropic(
            model=ctx.model,
            temperature=0,
            max_tokens=768,
            timeout=20,
            max_retries=0,
            tags=["langsmith:nostream"],
        )
        result = internal_model_call(
            client.with_structured_output(TurnUnderstanding),
            [("system", system), ("human", json.dumps(payload, ensure_ascii=False))],
        )
        return _resolve(TurnUnderstanding.model_validate(result), tokens, stage, "classified")
    except (APIError, ValueError, TimeoutError, ConnectionError):
        return TurnResult(TurnUnderstanding(), "model_error")
