"""The LLM understands every turn; the backend bounds data and effects."""

import json
from datetime import date
from types import SimpleNamespace

import pytest

from agents.collections.semantic import TurnUnderstanding, understand_turn

TODAY = date(2026, 10, 18)
CONTEXT = SimpleNamespace(provider="anthropic", model="claude-haiku-4-5-20251001")


@pytest.fixture
def model_call(monkeypatch):
    import langchain_anthropic

    from agents.collections import streaming

    calls = []
    response = {}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["max_retries"] == 0
            assert kwargs["tags"] == ["langsmith:nostream"]

        def with_structured_output(self, schema):
            assert schema is TurnUnderstanding
            return self

    def invoke(client, messages):
        calls.append(messages)
        if isinstance(response.get("result"), Exception):
            raise response["result"]
        return response.get("result", {})

    monkeypatch.setattr(langchain_anthropic, "ChatAnthropic", Client)
    monkeypatch.setattr(streaming, "internal_model_call", invoke)
    return calls, response


@pytest.mark.parametrize(
    "text,stage,intent",
    [
        ("hola", "awaiting_customer", "greeting"),
        ("no quiero", "awaiting_customer", "decline"),
        ("dale", "closed", "continue"),
        ("termina", "ready", "end"),
        ("no quiero pagar", "ready", "refuse"),
        ("qué me ofreces para pagar", "ready", "options"),
        ("qué acordamos antes", "ready", "history"),
        ("ayúdame con Python", "ready", "off_topic"),
        ("qué mal servicio", "handoff", "complaint"),
        ("mañana", "awaiting_terms", "unclear"),
        ("no puedo destinar nada", "ready", "no_capacity"),
    ],
)
def test_every_kind_of_production_turn_calls_model_once(model_call, text, stage, intent):
    calls, response = model_call
    response["result"] = {"intent": intent}
    result = understand_turn(text, ctx=CONTEXT, today=TODAY, stage=stage)
    assert result.status == "classified" and result.turn.intent == intent
    assert len(calls) == 1
    assert json.loads(calls[0][1][1])["current_message"] == text
    assert f"Trusted backend conversation stage: {stage}." in calls[0][0][1]


def test_entire_current_message_stays_data_and_history_is_bounded(model_call):
    calls, _ = model_call
    text = "a" * 1800 + " {{ 7 * 7 }} {% include 'secret' %} no acepto"
    history = [{"user": "123456789 " + "x" * 1000, "assistant": "1111"}] * 8
    understand_turn(
        text,
        ctx=CONTEXT,
        today=TODAY,
        stage="ready",
        history=history,
        context={
            "account_id": "SECRET",
            "pending_offer": {"offer_id": "SECRET", "total_cop": 600000},
            "draft_terms": {"payment_date": "2026-10-19", "amount_cop": 200000},
            "recoverable_misclosure": True,
        },
    )
    system = calls[0][0][1]
    payload = json.loads(calls[0][1][1])
    assert payload["current_message"] == text
    assert text not in system
    assert len(payload["recent_dialogue"]) == 4
    assert len(payload["recent_dialogue"][0]["user"]) == 600
    assert "123456789" not in calls[0][1][1]
    assert "SECRET" not in calls[0][1][1]
    assert payload["backend_context"]["pending_offer"] == {"total_cop": 600000}
    assert payload["backend_context"]["draft_terms"]["amount_cop"] == 200000
    assert payload["backend_context"]["draft_terms"]["payment_date"] == "2026-10-19"


@pytest.mark.parametrize(
    "stage,text,reply,field,expected",
    [
        (
            "awaiting_customer",
            "mi cédula es 00123456789",
            {"intent": "identity", "identity_token": 0},
            "identity_candidate",
            "00123456789",
        ),
        (
            "awaiting_identity",
            "el código que recibí es 1111",
            {"intent": "verification_code", "code_token": 0},
            "verification_candidate",
            "1111",
        ),
        (
            "awaiting_customer",
            "no 987654321, es 123456789",
            {"intent": "identity", "identity_token": 1},
            "identity_candidate",
            "123456789",
        ),
    ],
)
def test_resolves_only_exact_current_numeric_candidate(
    model_call, stage, text, reply, field, expected
):
    calls, response = model_call
    response["result"] = reply
    result = understand_turn(text, ctx=CONTEXT, today=TODAY, stage=stage)
    assert result.status == "classified"
    assert getattr(result, field) == expected
    assert expected not in calls[0][1][1]
    assert "<number_" in json.loads(calls[0][1][1])["current_message"]


@pytest.mark.parametrize(
    "text,stage,reply",
    [
        ("hola", "awaiting_customer", {"intent": "identity", "identity_token": 0}),
        ("123456789", "awaiting_customer", {"intent": "identity", "identity_token": 2}),
        ("123456789", "awaiting_customer", {"intent": "identity", "identity_token": -1}),
        ("123456789", "awaiting_customer", {"intent": "identity", "identity_token": True}),
        (
            "123456789",
            "awaiting_customer",
            {"intent": "identity", "identity_token": 0, "code_token": 0},
        ),
        ("123456789", "ready", {"intent": "identity", "identity_token": 0}),
        ("12345", "awaiting_identity", {"intent": "verification_code", "code_token": 0}),
        ("1111", "awaiting_identity", {"intent": "verification_code"}),
        ("hola", "ready", {"intent": "unclear", "account_id": "invented"}),
        ("hola", "ready", {"intent": "unclear", "identity_code": "1111"}),
    ],
)
def test_unbacked_identity_and_model_authority_fail_closed(model_call, text, stage, reply):
    _, response = model_call
    response["result"] = reply
    result = understand_turn(text, ctx=CONTEXT, today=TODAY, stage=stage)
    assert result.status == "model_error" and result.turn.intent == "unclear"
    assert result.identity_candidate is None and result.verification_candidate is None


@pytest.mark.parametrize(
    "fields,stage,expected",
    [
        ({}, "awaiting_confirmation", "unclear"),
        ({"acceptance": "conditional"}, "awaiting_confirmation", "unclear"),
        ({"acceptance": "unconditional_current_offer"}, "ready", "unclear"),
        (
            {"acceptance": "unconditional_current_offer", "installments": 2},
            "awaiting_confirmation",
            "unclear",
        ),
        ({"acceptance": "unconditional_current_offer"}, "awaiting_confirmation", "accept_offer"),
    ],
)
def test_acceptance_requires_scope_and_no_changed_terms(model_call, fields, stage, expected):
    _, response = model_call
    response["result"] = {"intent": "accept_offer", **fields}
    result = understand_turn("sí", ctx=CONTEXT, today=TODAY, stage=stage)
    assert result.turn.intent == expected
    assert result.turn.acceptance == fields.get("acceptance", "none")


def test_conditional_counteroffer_keeps_financial_meaning(model_call):
    _, response = model_call
    response["result"] = {
        "intent": "counteroffer",
        "acceptance": "conditional",
        "amount_cop": 150000,
        "amount_is_capacity": True,
    }
    result = understand_turn(
        "acepto solo si son 150 mil al mes", ctx=CONTEXT, today=TODAY, stage="awaiting_confirmation"
    )
    assert result.turn.intent == "counteroffer"
    assert result.turn.acceptance == "conditional"
    assert result.turn.amount_cop == 150000 and result.turn.amount_is_capacity


@pytest.mark.parametrize("error", [TimeoutError(), ConnectionError(), ValueError("malformed")])
def test_provider_failure_does_not_fallback_to_lexical_acceptance(model_call, error):
    calls, response = model_call
    response["result"] = error
    result = understand_turn("confirmo", ctx=CONTEXT, today=TODAY, stage="awaiting_confirmation")
    assert result.status == "model_error" and result.turn.intent == "unclear"
    assert result.turn.acceptance == "none"
    assert len(calls) == 1


def test_mock_adapter_is_explicitly_offline(model_call):
    calls, _ = model_call
    result = understand_turn(
        "dale",
        ctx=SimpleNamespace(provider="mock"),
        today=TODAY,
        stage="closed",
        context={"recoverable_misclosure": True},
    )
    assert result.status == "mock" and result.turn.intent == "continue"
    assert not calls


def test_mock_does_not_confuse_incidental_amount_with_identity(model_call):
    result = understand_turn(
        "quiero pagar 123 pesos",
        ctx=SimpleNamespace(provider="mock"),
        today=TODAY,
        stage="awaiting_customer",
    )
    assert result.identity_candidate is None
    assert result.turn.intent != "identity"


def test_frequency_is_semantic_and_latest_question_is_bounded(model_call):
    calls, response = model_call
    response["result"] = {"intent": "negotiate", "frequency": "monthly"}
    result = understand_turn(
        "cada mes",
        ctx=CONTEXT,
        today=TODAY,
        stage="awaiting_terms",
        context={"latest_assistant_prompt": "¿Ese monto es mensual? Código 1111 " + "x" * 800},
    )
    assert result.turn.frequency == "monthly"
    payload = json.loads(calls[0][1][1])
    assert len(payload["backend_context"]["latest_assistant_prompt"]) == 600
    assert "1111" not in calls[0][1][1]


def test_closed_unverified_turn_masks_current_numbers(model_call):
    calls, response = model_call
    response["result"] = {"intent": "complaint"}
    result = understand_turn(
        "mi cédula era 123456789 y mi código 1111",
        ctx=CONTEXT,
        today=TODAY,
        stage="closed",
        context={"identity_verified": False},
    )
    assert result.turn.intent == "complaint"
    assert "123456789" not in calls[0][1][1] and "1111" not in calls[0][1][1]
    response["result"] = {"intent": "identity", "identity_token": 0}
    result = understand_turn(
        "123456789", ctx=CONTEXT, today=TODAY, stage="closed", context={"identity_verified": False}
    )
    assert result.status == "model_error" and result.identity_candidate is None


@pytest.mark.parametrize(
    "fields", [{"frequency": "monthly"}, {"frequency": "one_off"}, {"amount_is_capacity": True}]
)
def test_changed_frequency_or_capacity_never_accepts_offer(model_call, fields):
    _, response = model_call
    response["result"] = {
        "intent": "accept_offer",
        "acceptance": "unconditional_current_offer",
        **fields,
    }
    result = understand_turn(
        "sí, pero mensual", ctx=CONTEXT, today=TODAY, stage="awaiting_confirmation"
    )
    assert result.turn.intent == "unclear"


def test_studio_text_content_blocks_preserve_recent_question(model_call):
    from langchain_core.messages import AIMessage

    calls, _ = model_call
    history = [
        AIMessage(content=[{"type": "text", "text": "¿Cuánto puedes destinar al mes?"}]),
        {"role": "human", "content": [{"type": "text", "text": "Mi número era 123456789"}]},
    ]
    understand_turn("nada", ctx=CONTEXT, today=TODAY, stage="awaiting_terms", history=history)
    recent = json.loads(calls[0][1][1])["recent_dialogue"]
    assert recent[0]["text"] == "¿Cuánto puedes destinar al mes?"
    assert recent[1]["text"] == "Mi número era [number]"


@pytest.mark.parametrize(
    "text,awaiting,intent,frequency",
    [
        ("nada", "capacity", "no_capacity", "unspecified"),
        ("0", "capacity", "no_capacity", "unspecified"),
        ("un único abono", "frequency", "promise", "one_off"),
        ("mensual", "frequency", "negotiate", "monthly"),
        ("un solo pago", "frequency", "promise", "one_off"),
    ],
)
def test_mock_contextual_capacity_and_frequency(model_call, text, awaiting, intent, frequency):
    result = understand_turn(
        text,
        ctx=SimpleNamespace(provider="mock"),
        today=TODAY,
        stage="awaiting_terms",
        context={"dialogue": {"awaiting": awaiting}},
    )
    assert result.turn.intent == intent and result.turn.frequency == frequency


@pytest.mark.parametrize(
    "intent,status", [("no_capacity", "classified"), ("promise", "model_error")]
)
def test_zero_only_normalizes_explicit_no_capacity(model_call, intent, status):
    _, response = model_call
    response["result"] = {"intent": intent, "amount_cop": 0}
    result = understand_turn("nada", ctx=CONTEXT, today=TODAY, stage="awaiting_terms")
    assert result.status == status
    assert result.turn.amount_cop is None
