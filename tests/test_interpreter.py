"""Extracción offline y límites de confianza, sin llamadas a proveedores."""

import sys
from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agents.collections.interpreter import ParsedTurn, parse_turn
from agents.collections.semantic import TurnUnderstanding, understand_turn

TODAY = date(2026, 10, 17)


@pytest.mark.parametrize(
    ("text", "intent", "amount", "payment_date", "installments"),
    [
        ("Pagaré $600000 mañana", "promise", 600000, date(2026, 10, 18), None),
        ("Pagaré 200.000, mañana", "promise", 200000, date(2026, 10, 18), None),
        ("Pago 600.000 el 2026-10-20", "promise", 600000, date(2026, 10, 20), None),
        ("Puedo pagar doscientos mil hoy", "promise", 200000, TODAY, None),
        ("Quiero pagar en tres cuotas desde mañana", "negotiate", None, date(2026, 10, 18), 3),
        ("Puedo pagar doscientos mil al mes", "counteroffer", 200000, None, None),
        ("Quiero 3 cuotas de 200.000 desde 2026-10-20", "negotiate", 200000, date(2026, 10, 20), 3),
        ("Pago un millón doscientos mil mañana", "promise", 1200000, date(2026, 10, 18), None),
        ("No puedo pagar", "hardship", None, None, None),
        ("Ya pagué", "reported_payment", None, None, None),
        ("No reconozco esa deuda", "dispute", None, None, None),
        ("Quiero hablar con un asesor", "request_human", None, None, None),
        ("Quiero terminar", "end", None, None, None),
        ("Puedo pagar el viernes", "promise", None, None, None),
    ],
)
def test_mock_extracts_supported_spanish(text, intent, amount, payment_date, installments):
    parsed = parse_turn(text, today=TODAY)
    assert parsed.intent == intent
    assert parsed.amount_cop == amount
    assert parsed.payment_date == payment_date
    assert parsed.installments == installments


@pytest.mark.parametrize(
    "text", ["Sí", "¡Sí!", "Sí, acepto", "Acepto el plan", "Confirmo la oferta"]
)
def test_acceptance_requires_entire_explicit_utterance(text):
    assert parse_turn(text, today=TODAY).intent == "accept_offer"


@pytest.mark.parametrize(
    "text",
    [
        "Si puedo pagar mañana, acepto",
        "Acepto una parte",
        "Acepto, pagaré 200000",
        "Tal vez acepto",
        "Depende",
        "No he pagado",
        "No quiero un asesor",
        "Pagaré 200000 o 300000 mañana",
        "Pagaré mañana o el 2026-10-20",
        "Pago el 2026-02-30",
        "Quiero 2 o 3 cuotas",
        "hola",
        "No quiero terminar",
        "si ya pagué entonces qué",
    ],
)
def test_ambiguous_or_negated_turns_do_not_authorize(text):
    assert parse_turn(text, today=TODAY).intent == "unclear"


def test_identity_is_only_declared_code():
    parsed = parse_turn("verificar 1234", today=TODAY)
    assert parsed == ParsedTurn(identity_code="1234")
    assert parse_turn("soy el titular, verifica mi identidad", today=TODAY).identity_code is None


@pytest.mark.parametrize(
    "payload",
    [
        {"amount_cop": True},
        {"amount_cop": -1},
        {"installments": 0},
        {"verified": True},
        {"intent": "paid"},
    ],
)
def test_schema_rejects_invalid_or_authoritative_fields(payload):
    with pytest.raises(ValidationError):
        ParsedTurn.model_validate(payload)


def _fake_provider(monkeypatch, output=None, error=None):
    observed = {}

    class FakeChatAnthropic:
        def __init__(self, **kwargs):
            observed.update(kwargs)

        def with_structured_output(self, schema):
            assert schema is TurnUnderstanding
            return self

        def invoke(self, messages):
            observed["messages"] = messages
            if error:
                raise error
            return output

    monkeypatch.setitem(
        sys.modules, "langchain_anthropic", SimpleNamespace(ChatAnthropic=FakeChatAnthropic)
    )
    return observed


def production_turn(text, *, stage="awaiting_confirmation"):
    return understand_turn(
        text,
        ctx=SimpleNamespace(provider="anthropic", model="claude-haiku-4-5-20251001"),
        today=TODAY,
        stage=stage,
    )


def test_anthropic_uses_haiku_and_cannot_promote_conditional_acceptance(monkeypatch):
    observed = _fake_provider(monkeypatch, {"intent": "accept_offer", "acceptance": "conditional"})
    result = production_turn("acepto si me dan un descuento")
    assert result.turn.intent == "unclear"
    assert observed["model"] == "claude-haiku-4-5-20251001"
    assert observed["max_retries"] == 0


def test_model_cannot_invent_identity_code(monkeypatch):
    _fake_provider(monkeypatch, {"intent": "unclear", "identity_code": "1234"})
    result = production_turn("soy yo", stage="awaiting_identity")
    assert result.status == "model_error" and result.verification_candidate is None


def test_provider_errors_are_not_hidden(monkeypatch):
    _fake_provider(monkeypatch, error=TimeoutError("provider unavailable"))
    result = production_turn("hola")
    assert result.status == "model_error" and result.turn.intent == "unclear"


def test_unknown_provider_is_not_silently_mocked():
    with pytest.raises(ValueError, match="mock-only; use understand_turn"):
        parse_turn("hola", today=TODAY, provider="unknown")


def test_acceptance_cannot_smuggle_changed_financial_fields(monkeypatch):
    _fake_provider(
        monkeypatch,
        {
            "intent": "accept_offer",
            "acceptance": "unconditional_current_offer",
            "amount_cop": 100,
            "installments": 5,
        },
    )
    assert production_turn("acepto").turn.intent == "unclear"


def test_provider_output_is_schema_validated(monkeypatch):
    _fake_provider(monkeypatch, {"intent": "promise", "amount_cop": -1})
    result = production_turn("pagaré mañana")
    assert result.status == "model_error" and result.turn.intent == "unclear"


@pytest.mark.parametrize(
    "text,expected",
    [("mañana", date(2026, 10, 18)), ("hoy", TODAY), ("2026-10-20", date(2026, 10, 20))],
)
def test_isolated_date_can_complete_existing_draft(text, expected):
    assert parse_turn(text, today=TODAY) == ParsedTurn(payment_date=expected)


@pytest.mark.parametrize("text", ["¿confirmo?", "confirmo?", "¿acepto?", "sí?"])
def test_question_is_not_confirmation(text):
    assert parse_turn(text, today=TODAY).intent != "accept_offer"


@pytest.mark.parametrize(
    "text,amount",
    [
        ("pago 200 mil mañana", 200000),
        ("pago 2 millones mañana", 2000000),
        ("pago $200 mil mañana", 200000),
    ],
)
def test_mixed_numeric_word_amount(text, amount):
    parsed = parse_turn(text, today=TODAY)
    assert parsed.intent == "promise"
    assert parsed.amount_cop == amount
    assert parsed.payment_date == date(2026, 10, 18)


def test_negated_date_is_not_extracted_as_a_draft_update():
    assert parse_turn("no pagaré mañana", today=TODAY) == ParsedTurn()


def test_anthropic_question_cannot_confirm(monkeypatch):
    _fake_provider(monkeypatch, {"intent": "accept_offer"})
    assert production_turn("¿confirmo?").turn.intent == "unclear"


@pytest.mark.parametrize(
    "text,intent",
    [
        ("Sí, pero solo dos cuotas", "counteroffer"),
        ("no acepto", "reject_offer"),
        ("no quiero pagar nada", "refuse"),
        ("acepto si me descuentan", "request_exception"),
    ],
)
def test_objections_and_counteroffers_are_not_acceptance(text, intent):
    assert parse_turn(text, today=TODAY).intent == intent


@pytest.mark.parametrize("text", ["confirmo", "mañana", "terminar", "hola", "verificar 1111"])
def test_fixture_helpers_reject_production_before_any_lexical_work(monkeypatch, text):
    from agents.collections import interpreter, onboarding

    def forbidden(*args, **kwargs):
        raise AssertionError("Production input reached a fixture parser")

    monkeypatch.setattr(interpreter, "_parse_mock", forbidden)
    monkeypatch.setattr(onboarding, "normalize_text", forbidden)
    with pytest.raises(ValueError, match="mock-only; use understand_turn"):
        parse_turn(text, today=TODAY, provider="anthropic")
    with pytest.raises(ValueError, match="mock-only; use understand_turn"):
        onboarding.classify_onboarding(
            text, SimpleNamespace(provider="anthropic", stream_responses=True)
        )
