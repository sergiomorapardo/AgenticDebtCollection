"""One semantic interpretation per human turn; all authorization stays in the backend."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from anthropic import APIError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents.collections.numbers import NumberEvidence, normalize_transcript_numbers
from agents.collections.prompts import PromptStage, render_prompt

_WEEKDAYS = ("lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo")
SPANISH_MONTHS = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)


class PaymentDateReference(BaseModel):
    """Calendar components supplied by the customer, not an inferred complete date."""

    model_config = ConfigDict(extra="forbid")
    quote: str = Field(min_length=1, max_length=160)
    day: int | None = Field(default=None, ge=1, le=31, strict=True)
    month: int | None = Field(default=None, ge=1, le=12, strict=True)
    year: int | None = Field(default=None, ge=1900, le=9999, strict=True)
    weekday: int | None = Field(default=None, ge=0, le=6, strict=True)
    occurrence: Literal["unspecified", "this", "next"] = "unspecified"


def _date_references(text, context):
    """Recognize bounded Spanish calendar forms, retaining their complete source spans."""
    from agents.collections.conversation import normalize_text

    normalized = normalize_text(normalize_transcript_numbers(text)).strip(" .!?¡¿")
    dialogue = context.get("dialogue", {})
    pending = dialogue.get("pending_date") if dialogue.get("awaiting") == "date" else None
    terms = context.get("draft_terms", {})
    awaiting_date = bool(pending) or (
        not terms.get("payment_date")
        and (terms.get("installments") or terms.get("route") == "promise")
    )
    references = []
    occupied = []

    def add(match, **fields):
        if any(match.start() < end and match.end() > start for start, end in occupied):
            return
        occupied.append(match.span())
        try:
            references.append(PaymentDateReference(quote=match[0], **fields))
        except ValueError:
            references.append(PaymentDateReference(quote=match[0]))

    for match in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", normalized):
        add(match, year=int(match[1]), month=int(match[2]), day=int(match[3]))
    for match in re.finditer(r"\b(pasado manana|manana|hoy)\b", normalized):
        value = context["today"] + timedelta(
            days={"hoy": 0, "manana": 1, "pasado manana": 2}[match[1]]
        )
        add(match, day=value.day, month=value.month, year=value.year)
    month_pattern = "|".join(SPANISH_MONTHS)
    for match in re.finditer(
        rf"\b(?:el\s+|dia\s+)?(\d{{1,2}})\s+de\s+({month_pattern})(?:\s+(?:de\s+)?(\d{{4}}))?\b",
        normalized,
    ):
        add(
            match,
            day=int(match[1]),
            month=SPANISH_MONTHS.index(match[2]) + 1,
            year=int(match[3]) if match[3] else None,
        )
    weekday_pattern = "|".join(_WEEKDAYS)
    for match in re.finditer(
        rf"\b(?:(el|este|proximo|el proximo)\s+)?({weekday_pattern})(\s+que viene)?\b", normalized
    ):
        occurrence = (
            "next"
            if match[3] or "proximo" in (match[1] or "")
            else "this"
            if match[1] == "este"
            else "unspecified"
        )
        add(match, weekday=_WEEKDAYS.index(match[2]), occurrence=occurrence)
    for match in re.finditer(
        r"\b(?:el|dia)\s+(\d{1,2})(?!\d|\s*(?:pesos|cop|mil|cuotas))\b", normalized
    ):
        add(match, day=int(match[1]))
    if not references and awaiting_date:
        if match := re.fullmatch(r"(\d{1,2})", normalized):
            add(match, day=int(match[1]))
        elif pending and pending.get("day"):
            if match := re.fullmatch(
                rf"(?:de\s+)?({month_pattern})(?:\s+(?:de\s+)?(\d{{4}}))?", normalized
            ):
                add(
                    match,
                    day=pending["day"],
                    month=SPANISH_MONTHS.index(match[1]) + 1,
                    year=int(match[2]) if match[2] else None,
                )
            elif pending.get("month") and (match := re.fullmatch(r"(?:de\s+)?(\d{4})", normalized)):
                add(match, day=pending["day"], month=pending["month"], year=int(match[1]))
        elif (
            pending
            and pending.get("weekday") is not None
            and normalized in {"el proximo", "proximo", "la proxima semana", "este", "esta semana"}
        ):
            references.append(
                PaymentDateReference(
                    quote=normalized,
                    weekday=pending["weekday"],
                    occurrence="this" if normalized in {"este", "esta semana"} else "next",
                )
            )
    return references, normalized


def _resolve_date_reference(reference, today):
    from agents.collections.policy import load_policy

    window = [
        today + timedelta(days=offset)
        for offset in range(load_policy().first_payment_within_days + 1)
    ]
    if reference.weekday is not None:
        offset = (reference.weekday - today.weekday()) % 7
        if reference.occurrence == "next" and offset == 0:
            offset = 7
        if reference.occurrence == "unspecified" and offset == 0:
            return None, "weekday"
        return today + timedelta(days=offset), None
    if reference.day is None:
        return None, "invalid"
    if reference.month is not None and reference.year is not None:
        try:
            return date(reference.year, reference.month, reference.day), None
        except ValueError:
            return None, "invalid"
    candidates = [
        value
        for value in window
        if value.day == reference.day
        and (reference.month is None or value.month == reference.month)
    ]
    if len(candidates) == 1:
        return candidates[0], None
    if reference.month is not None:
        # February 29 can be valid in a different year; April 31 never can.
        try:
            date(2000, reference.month, reference.day)
        except ValueError:
            return None, "invalid"
        return None, "year"
    return None, "month"


def _ground_payment_date(turn, text, today, stage, context):
    """Resolve partial dates locally; the model cannot invent their missing components."""
    if (
        stage not in {"ready", "awaiting_terms", "awaiting_confirmation"}
        or context.get("identity_verified") is False
    ):
        return turn.model_copy(update={"date_reference": None, "date_clarification": None})
    references, normalized = _date_references(text, {**context, "today": today})
    dialogue = context.get("dialogue", {})
    pending = dialogue.get("pending_date") if dialogue.get("awaiting") == "date" else None
    date_only = normalized
    for item in references:
        date_only = date_only.replace(item.quote, "", 1)
    if turn.intent not in {
        "unclear",
        "promise",
        "negotiate",
        "counteroffer",
        "accept_offer",
        "continue",
    } and not (
        turn.intent == "clarify_process"
        and dialogue.get("awaiting") == "date"
        and references
        and not date_only.strip()
    ):
        return turn.model_copy(update={"date_reference": None, "date_clarification": None})
    pending_confirmation = False
    if (
        not references
        and pending
        and normalized in {"si", "confirmo", "acepto", "si confirmo", "si acepto"}
    ):
        references = [PaymentDateReference.model_validate(pending)]
        pending_confirmation = True
    if not references:
        if turn.date_reference is not None:
            raise ValueError("Date reference is not grounded in a supported current expression")
        return turn.model_copy(update={"date_clarification": None})
    reference = references[0]
    # The backend's complete current expression wins over guessed components.
    if (
        not pending_confirmation
        and turn.date_reference is not None
        and turn.date_reference.quote not in text
    ):
        raise ValueError("Date evidence is not in the current message")
    resolved, question = (
        _resolve_date_reference(reference, today) if len(references) == 1 else (None, "multiple")
    )
    if re.search(r"\b(?:o|sino)\b|\bno\s+(?:el|este|proximo|hoy|manana)", normalized):
        resolved, question = None, "multiple"
    if pending_confirmation:
        resolved, question = None, question or "multiple"
    remainder_for_qualifiers = normalized
    for item in references:
        remainder_for_qualifiers = remainder_for_qualifiers.replace(item.quote, "", 1)
    if re.search(
        r"\b(?:proxim[oa]|siguiente|otr[oa]|est[ea]|pasad[oa])\s+(?:mes|semana)\b|\b(?:mes|semana)\s+que viene\b|\b(?:de|del)\s+\d{4}\b",
        remainder_for_qualifiers,
    ):
        resolved, question = None, "multiple"
    updates = {
        "date_reference": reference,
        "date_clarification": question,
        "payment_date": resolved,
        "acceptance": "none",
    }
    remainder = normalized
    for item in references:
        remainder = remainder.replace(item.quote, "", 1)
    # A day of the month must never become money or an installment count.
    for field, evidence_name in (
        ("amount_cop", "amount_evidence"),
        ("installments", "installments_evidence"),
    ):
        evidence = getattr(turn, evidence_name)
        if getattr(turn, field) is not None and (
            evidence is None
            or any(
                normalize_transcript_numbers(evidence.quote) in item.quote for item in references
            )
        ):
            value = getattr(turn, field)
            if field == "installments" and re.search(rf"\b{value}\s+cuotas?\b", remainder):
                continue
            if field == "amount_cop" and re.search(rf"(?<![\w\d]){value}(?![\w\d])", remainder):
                continue
            # An explicit full-payment request infers a single installment.
            if (
                field == "installments"
                and evidence is None
                and turn.installments == 1
                and turn.intent == "promise"
                and turn.frequency == "one_off"
            ):
                continue
            updates.update({field: None, evidence_name: None})
    if not remainder.strip() or pending_confirmation:
        updates.update(
            intent="unclear",
            frequency="unspecified",
            amount_cop=None,
            amount_evidence=None,
            installments=None,
            installments_evidence=None,
            amount_is_capacity=False,
        )
    elif turn.intent in {"accept_offer", "continue"}:
        updates["intent"] = "unclear"
    return turn.model_copy(update=updates)


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
    ] = Field(
        default="unclear",
        description="Current meaning. Selecting installments from an informational menu is negotiate, NOT accept_offer: for example la de tres cuotas, empezando mañana. Acknowledging an ambiguous menu without choosing is continue/unclear. Positive limited monthly capacity is counteroffer/negotiate, never no_capacity. no_capacity requires explicitly zero available money.",
    )
    amount_cop: int | None = Field(
        default=None,
        gt=0,
        strict=True,
        description="Positive COP amount explicitly available/proposed NOW: 250 mil = 250000 even after hardship. Preserve it; do not copy prior amounts. no_capacity requires null.",
    )
    amount_is_capacity: bool = Field(
        default=False,
        description="True for an explicitly stated maximum affordable monthly amount; limited positive capacity is not zero capacity.",
    )
    frequency: Literal["monthly", "one_off", "unspecified"] = "unspecified"
    payment_date: date | None = Field(
        default=None,
        description="Date explicitly supplied in this turn, resolving relative dates from today. A date-only answer completes existing terms; it does not replace the plan with a full-payment promise.",
    )
    date_reference: PaymentDateReference | None = Field(
        default=None,
        description="Current partial calendar expression and only its explicit components. el viernes: weekday=4, quote=el viernes; el 20: day=20, quote=el 20, month/year=null. Backend resolves dates and asks when ambiguous.",
    )
    date_clarification: Literal["month", "year", "weekday", "invalid", "multiple"] | None = None
    installments: int | None = Field(default=None, gt=0, strict=True)
    amount_evidence: NumberEvidence | None = Field(
        default=None,
        description="Required whenever amount_cop is present. Quote the complete current numeric expression; uno means integer 1, never the string uno.",
    )
    installments_evidence: NumberEvidence | None = Field(
        default=None,
        description="Required for an explicit installment count. uno/una means 1. May be null only for an inferred single installment from an explicit full-payment request.",
    )
    acceptance: Literal["none", "conditional", "unconditional_current_offer"] = Field(
        default="none",
        description="Selecting a menu option, supplying a date or choosing installment count is none: it requests a proposal, not an agreement. unconditional_current_offer requires an existing backend pending_offer, awaiting_confirmation stage and unconditional consent to that exact presented schedule without new terms.",
    )
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

    @model_validator(mode="after")
    def reject_contradictory_capacity(self):
        if self.intent == "no_capacity" and self.amount_cop is not None:
            raise ValueError("Positive available capacity contradicts no_capacity")
        return self


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
        if key == "dialogue" and isinstance(value.get("pending_date"), dict):
            selected["pending_date"] = PaymentDateReference.model_validate(
                value["pending_date"]
            ).model_dump(mode="json")
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
        turn = TurnUnderstanding.model_validate(
            {**turn.model_dump(), "intent": "unclear", "acceptance": "none"}
        )
    elif turn.intent != "accept_offer" and turn.acceptance == "unconditional_current_offer":
        turn = TurnUnderstanding.model_validate({**turn.model_dump(), "acceptance": "none"})
    return TurnResult(turn, status, identity, code)


def _mock_turn(text, *, today, stage, ctx, tokens, context):
    """Limited offline fixtures only; production never uses these lexical shortcuts."""
    from agents.collections.conversation import normalize_text, requests_end
    from agents.collections.interpreter import parse_turn
    from agents.collections.onboarding import classify_onboarding

    if requests_end(text):
        return TurnUnderstanding(intent="end")
    normalized = normalize_text(text)
    if re.search(r"\b(historial|resumen|acordamos|acordado)\b", normalized):
        return TurnUnderstanding(intent="history")
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
    if (
        context.get("dialogue", {}).get("awaiting") == "capacity"
        and fields["intent"] == "unclear"
        and fields["amount_cop"] is not None
    ):
        fields.update(intent="counteroffer", amount_is_capacity=True)
    if fields["intent"] == "promise" and re.search(
        r"\b(pago completo|pagar (?:el )?total|pagar todo|todo el saldo)\b", normalized
    ):
        fields.update(installments=1, frequency="one_off")
    if fields["intent"] == "accept_offer":
        if stage == "awaiting_confirmation":
            fields["acceptance"] = "unconditional_current_offer"
        else:
            fields["intent"] = "unclear" if stage in {"closed", "agreed", "handoff"} else "continue"
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
    system += """
Numeric output contract (required, including the examples above):
Whenever amount_cop is non-null, also return amount_evidence={"quote": "exact numeric expression"}.
Whenever installments is explicitly stated, also return installments_evidence with its expression.
Do NOT omit a stated value to avoid supplying evidence. Extract uno/un/una as integer 1.
"Puedo dar 1 peso al mes" => amount_cop=1, amount_evidence={"quote":"1"}, installments=null.
"Quiero una cuota" => installments=1, installments_evidence={"quote":"una"}, amount_cop=null.
"Puedo pagar doscientos mil pesos al mes" => amount_cop=200000,
amount_evidence={"quote":"doscientos mil"}. Include all scale words in the quote, no unit labels.
"250 mil" => amount_cop=250000, amount_evidence={"quote":"250 mil"}; never extract just 250 or mil.
"1" after asking how many installments => installments=1, installments_evidence={"quote":"1"}.
Date output contract: Do not guess missing month/year in partial dates. Return date_reference
with the exact current quote and explicitly supplied components; weekday uses Monday=0.
For el viernes return weekday=4 and occurrence=unspecified, with day/month/year null.
For el 20 return day=20 with month/year/weekday null. A date-only answer is not acceptance.
The backend resolves the calendar, asks for missing information and requires the full offer
before a commitment. Preserve the chosen plan. Use pending_date only to complete its fields.
Dates and identifiers are not monetary amounts. Keep identifiers as numeric_candidates references.
"""
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
            turn = _ground_payment_date(turn, text, today, stage, context or {})
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
        turn = TurnUnderstanding.model_validate(result)
        turn = _ground_payment_date(turn, text, today, stage, context or {})
        for field, evidence in (
            ("amount_cop", turn.amount_evidence),
            ("installments", turn.installments_evidence),
        ):
            value = getattr(turn, field)
            if value is None:
                if evidence is not None:
                    raise ValueError("Evidence without a numeric value")
                continue
            if evidence is None:
                if (
                    field == "installments"
                    and value == 1
                    and turn.intent == "promise"
                    and turn.frequency == "one_off"
                    and not re.search(r"\b(no|nunca|jamás)\b", text.lower())
                    and re.search(
                        r"\b(pago completo|pagar (?:el )?total|pagar todo|todo el saldo)\b",
                        text.lower(),
                    )
                ):
                    continue
                raise ValueError("Numeric value requires current-message evidence")
            evidence.verify(text, value)
        return _resolve(turn, tokens, stage, "classified")
    except (APIError, ValueError, TimeoutError, ConnectionError):
        return TurnResult(TurnUnderstanding(), "model_error")
