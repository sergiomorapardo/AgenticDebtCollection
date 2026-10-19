"""Offline fixture parser only; production meaning belongs to semantic.understand_turn.

The limited mock parser preserves deterministic tests without external provider calls.
Its output never authorizes identity, offers, agreements or payments.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agents.collections.conversation import requests_end, social_intent


class ParsedTurn(BaseModel):
    """Datos declarados por el interlocutor, nunca hechos verificados."""

    model_config = ConfigDict(extra="forbid")

    intent: Literal[
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
        "reject_offer",
        "counteroffer",
        "request_exception",
        "off_topic",
        "complaint",
        "clarify_process",
    ] = "unclear"
    amount_cop: int | None = Field(default=None, gt=0, strict=True)
    amount_is_capacity: bool = False
    payment_date: date | None = None
    installments: int | None = Field(default=None, gt=0, strict=True)
    identity_code: str | None = Field(default=None, pattern=r"^\d{4}$")


_ACCEPTANCES = {
    "si",
    "acepto",
    "confirmo",
    "si acepto",
    "si confirmo",
    "acepto la oferta",
    "acepto el plan",
    "confirmo la oferta",
    "confirmo el plan",
    "confirmo el acuerdo",
    "si acepto la oferta",
    "si acepto el plan",
    "estoy de acuerdo",
}
_NUMBERS = {
    "un": 1,
    "uno": 1,
    "una": 1,
    "dos": 2,
    "tres": 3,
    "cuatro": 4,
    "cinco": 5,
    "seis": 6,
    "siete": 7,
    "ocho": 8,
    "nueve": 9,
    "diez": 10,
    "once": 11,
    "doce": 12,
    "trece": 13,
    "catorce": 14,
    "quince": 15,
    "dieciseis": 16,
    "diecisiete": 17,
    "dieciocho": 18,
    "diecinueve": 19,
    "veinte": 20,
    "veintiuno": 21,
    "veintidos": 22,
    "veintitres": 23,
    "veinticuatro": 24,
    "veinticinco": 25,
    "veintiseis": 26,
    "veintisiete": 27,
    "veintiocho": 28,
    "veintinueve": 29,
    "treinta": 30,
    "cuarenta": 40,
    "cincuenta": 50,
    "sesenta": 60,
    "setenta": 70,
    "ochenta": 80,
    "noventa": 90,
    "cien": 100,
    "ciento": 100,
    "doscientos": 200,
    "trescientos": 300,
    "cuatrocientos": 400,
    "quinientos": 500,
    "seiscientos": 600,
    "setecientos": 700,
    "ochocientos": 800,
    "novecientos": 900,
}
_NUMBER_WORD = "|".join(sorted(_NUMBERS, key=len, reverse=True))
_WORD_AMOUNT = re.compile(
    rf"\b(?:(?:{_NUMBER_WORD})\s+(?:y\s+)?)*(?:mil|millon|millones)"
    rf"(?:\s+(?:{_NUMBER_WORD}|mil|y))*(?:\b)"
)


def _normalize(text: str) -> str:
    return " ".join(
        "".join(
            character
            for character in unicodedata.normalize("NFD", text.lower())
            if not unicodedata.combining(character)
        ).split()
    )


def _accepts_exactly(text: str) -> bool:
    if "?" in text or "¿" in text:
        return False
    normalized = _normalize(text).strip(" .,;:!¡")
    normalized = " ".join(normalized.replace(",", " ").split())
    return normalized in _ACCEPTANCES


def _word_amount(text: str) -> int:
    total = current = 0
    for word in text.split():
        if word == "mil":
            total += (current or 1) * 1000
            current = 0
        elif word in {"millon", "millones"}:
            total = (total + current or 1) * 1_000_000
            current = 0
        else:
            current += _NUMBERS.get(word, 0)
    return total + current


def _extract_fields(text: str, today: date) -> tuple[dict, bool]:
    fields: dict = {}
    dates: list[date] = []
    ambiguous = False
    for raw in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text):
        try:
            dates.append(date.fromisoformat(raw))
        except ValueError:
            ambiguous = True
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", text)
    for relative in re.findall(r"\b(?:pasado manana|manana|hoy)\b", text):
        dates.append(today + timedelta(days={"hoy": 0, "manana": 1, "pasado manana": 2}[relative]))
    if len(dates) == 1:
        fields["payment_date"] = dates[0]
    elif len(dates) > 1:
        ambiguous = True

    counts = re.findall(rf"\b(\d+|{_NUMBER_WORD})\s+cuotas?\b", text)
    if len(counts) == 1:
        count = int(counts[0]) if counts[0].isdigit() else _NUMBERS[counts[0]]
        if count > 0:
            fields["installments"] = count
        else:
            ambiguous = True
    elif len(counts) > 1:
        ambiguous = True
    text = re.sub(rf"\b(\d+|{_NUMBER_WORD})\s+cuotas?\b", " ", text)

    scaled_pattern = re.compile(
        r"(?<![\w.,-])\$?\s*(\d{1,3}(?:\.\d{3})+|\d+)\s+(mil|millon(?:es)?)\b"
    )
    amounts = [
        int(match.group(1).replace(".", "")) * (1000 if match.group(2) == "mil" else 1_000_000)
        for match in scaled_pattern.finditer(text)
    ]
    text = scaled_pattern.sub(" ", text)
    amounts.extend(_word_amount(match.group()) for match in _WORD_AMOUNT.finditer(text))
    text = _WORD_AMOUNT.sub(" ", text)
    for match in re.finditer(r"(?<![\w.,-])\$?\s*(\d{1,3}(?:\.\d{3})+|\d+)(?![\w-]|[.,]\d)", text):
        amount = int(match.group(1).replace(".", ""))
        if amount > 0:
            amounts.append(amount)
        else:
            ambiguous = True
    if len(amounts) == 1:
        fields["amount_cop"] = amounts[0]
    elif len(amounts) > 1:
        ambiguous = True
    return fields, ambiguous


def _parse_mock(text: str, today: date) -> ParsedTurn:
    normalized = _normalize(text).strip(" .,;:!¡¿?")
    identity = re.fullmatch(r"verificar\s+(\d{4})", normalized)
    if identity:
        return ParsedTurn(identity_code=identity.group(1))
    if _accepts_exactly(text):
        return ParsedTurn(intent="accept_offer")
    if requests_end(text):
        return ParsedTurn(intent="end")
    boundary = social_intent(text)
    if boundary:
        return ParsedTurn(intent=boundary)
    if re.search(r"\b(no reconozco|desconozco|no es mia|numero equivocado)\b", normalized):
        return ParsedTurn(intent="dispute")
    if re.search(r"\b(descuento|descuentan|condonar|condonacion)\b", normalized):
        return ParsedTurn(intent="request_exception")
    fields, ambiguous = _extract_fields(normalized, today)
    conditional = re.search(r"\b(solo si|depende|quizas|tal vez)\b", normalized)
    capacity = bool(re.search(r"\b(al mes|mensual|mensuales)\b", normalized))
    if (
        not ambiguous
        and not conditional
        and fields
        and re.search(r"\b(solo puedo|puedo pagar|prefiero|mejor|pero solo)\b", normalized)
        and not re.search(r"\b(no puedo|no pagare|si puedo)\b", normalized)
        and (capacity or re.search(r"\b(solo puedo|pero solo|mejor|prefiero)\b", normalized))
    ):
        return ParsedTurn(intent="counteroffer", amount_is_capacity=capacity, **fields)
    if re.search(r"\b(no quiero pagar|no voy a pagar|no pagare nada)\b", normalized):
        return ParsedTurn(intent="refuse")
    if re.search(r"\b(no acepto|no me sirve|no me funciona|muy alta|demasiado alta)\b", normalized):
        return ParsedTurn(intent="reject_offer")
    if re.search(
        r"\b(no puedo pagar|sin trabajo|sin empleo|sin ingresos|no tengo dinero|perdi mi trabajo)\b",
        normalized,
    ):
        return ParsedTurn(intent="hardship")
    if (
        re.fullmatch(r"(?:\$?\s*[\d.,]+(?: mil)?|[a-z ]+ mil)(?: al mes| mensuales)?", normalized)
        and fields
        and not ambiguous
    ):
        return ParsedTurn(amount_is_capacity=capacity, **fields)
    if re.search(r"\b(si|pero|quizas|tal vez|depende|solo si|acepto|confirmo|o)\b", normalized):
        return ParsedTurn()
    if re.search(r"\b(no reconozco|desconozco|disputo|no es mia|numero equivocado)\b", normalized):
        return ParsedTurn(intent="dispute")
    if re.search(
        r"\b(quiero|necesito|prefiero) (?:hablar con |un |una )*(asesor|persona|humano)\b",
        normalized,
    ) and not re.search(r"\bno (?:quiero|necesito|prefiero)\b", normalized):
        return ParsedTurn(intent="request_human")
    if re.search(
        r"\b(ya pague|ya he pagado|ya realice el pago|ya hice el pago)\b", normalized
    ) and not re.search(r"\b(no|nunca)\b", normalized):
        return ParsedTurn(intent="reported_payment")
    fields, ambiguous = _extract_fields(normalized, today)
    if ambiguous or re.search(r"\b(si|pero|quizas|tal vez|depende|solo si)\b", normalized):
        return ParsedTurn()
    if re.fullmatch(r"(?:hoy|manana|pasado manana|\d{4}-\d{2}-\d{2})", normalized):
        return ParsedTurn(**fields)
    if re.search(r"\b(no acepto|no confirmo|no prometo|no voy a pagar)\b", normalized):
        return ParsedTurn()
    if re.search(
        r"\b(cuotas?|negociar|plan de pagos?|no puedo pagar|al mes|mensual)\b", normalized
    ):
        return ParsedTurn(intent="negotiate", amount_is_capacity=capacity, **fields)
    if re.search(
        r"\b(pagare|pago|pagar|prometo|promesa|abonar|abono)\b", normalized
    ) and not re.search(r"\b(no|nunca)\b", normalized):
        return ParsedTurn(intent="promise", **fields)
    return ParsedTurn()


def parse_turn(
    text: str,
    *,
    today: date,
    provider: str = "mock",
    model: str = "claude-haiku-4-5-20251001",
    context: str = "",
) -> ParsedTurn:
    """Parse supported offline fixtures; production callers must use understand_turn.

    Retain model/context parameters for compatibility, but never use them to select
    or invoke a provider. Reject non-mock usage before interpreting any text.
    """
    if provider != "mock":
        raise ValueError(
            "parse_turn is mock-only; use understand_turn for production interpretation"
        )
    return _parse_mock(text, today)
