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
    assert result.turn.acceptance == (
        "unconditional_current_offer" if expected == "accept_offer" else "none"
    )


def test_conditional_counteroffer_keeps_financial_meaning(model_call):
    _, response = model_call
    response["result"] = {
        "intent": "counteroffer",
        "acceptance": "conditional",
        "amount_cop": 150000,
        "amount_evidence": {"quote": "150 mil"},
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


def test_positive_capacity_cannot_be_discarded_as_no_capacity(model_call):
    _, response = model_call
    response["result"] = {
        "intent": "no_capacity",
        "amount_cop": 250000,
        "amount_is_capacity": True,
        "frequency": "monthly",
    }
    result = understand_turn(
        "solo puedo destinar 250 mil al mes", ctx=CONTEXT, today=TODAY, stage="ready"
    )
    assert result.status == "model_error" and result.turn.intent == "unclear"
    assert result.turn.amount_cop is None


def test_positive_capacity_survives_prior_refusal_and_limited_funds(model_call):
    calls, response = model_call
    response["result"] = {
        "intent": "counteroffer",
        "amount_cop": 250000,
        "amount_evidence": {"quote": "250 mil"},
        "amount_is_capacity": True,
        "frequency": "monthly",
    }
    result = understand_turn(
        "solo puedo destinar 250 mil al mes",
        ctx=CONTEXT,
        today=TODAY,
        stage="awaiting_terms",
        history=[{"user": "no puedo pagar nada", "assistant": "¿Qué opción podemos revisar?"}],
    )
    assert result.turn.intent == "counteroffer" and result.turn.amount_cop == 250000
    assert result.turn.amount_is_capacity and result.turn.frequency == "monthly"
    assert json.loads(calls[0][1][1])["current_message"] == "solo puedo destinar 250 mil al mes"


def test_date_only_turn_preserves_extracted_date_without_inventing_replacement_plan(model_call):
    _, response = model_call
    response["result"] = {"intent": "unclear", "payment_date": "2026-10-19"}
    result = understand_turn(
        "mañana",
        ctx=CONTEXT,
        today=TODAY,
        stage="awaiting_terms",
        context={
            "draft_terms": {"installments": 3, "amount_cop": 250000, "amount_is_capacity": True},
            "dialogue": {"awaiting": "date"},
        },
    )
    assert result.turn.payment_date == date(2026, 10, 19)
    assert result.turn.intent == "unclear"
    assert result.turn.amount_cop is None and result.turn.installments is None


def test_offline_explicit_full_payment_selects_one_installment():
    result = understand_turn(
        "pago completo el 2026-10-21",
        ctx=SimpleNamespace(provider="mock"),
        today=TODAY,
        stage="awaiting_terms",
        context={"draft_terms": {"installments": 3}},
    )
    assert result.turn.intent == "promise"
    assert result.turn.installments == 1 and result.turn.frequency == "one_off"
    assert result.turn.payment_date == date(2026, 10, 21)


@pytest.mark.parametrize("intent", ["negotiate", "promise", "counteroffer", "unclear"])
@pytest.mark.parametrize("stage", ["ready", "awaiting_confirmation"])
def test_other_intents_cannot_carry_unconditional_acceptance(model_call, intent, stage):
    _, response = model_call
    response["result"] = {
        "intent": intent,
        "acceptance": "unconditional_current_offer",
        "amount_cop": 200000,
        "amount_evidence": {"quote": "200 mil"},
    }
    result = understand_turn("propondría 200 mil", ctx=CONTEXT, today=TODAY, stage=stage)
    assert result.status == "classified"
    assert result.turn.intent == intent
    assert result.turn.acceptance == "none"
    assert result.turn.amount_cop == 200000


@pytest.mark.parametrize(
    "quote,value",
    [
        ("uno", 1),
        ("una", 1),
        ("250 mil", 250000),
        ("doscientos mil", 200000),
        ("1,5 millones", 1500000),
        ("600.000", 600000),
        ("600,000", 600000),
    ],
)
def test_numbers_are_strict_integers_grounded_in_current_words(model_call, quote, value):
    _, response = model_call
    response["result"] = {
        "intent": "negotiate",
        "amount_cop": value,
        "amount_evidence": {"quote": quote},
    }
    result = understand_turn(f"puedo pagar {quote} pesos", ctx=CONTEXT, today=TODAY, stage="ready")
    assert result.status == "classified"
    assert type(result.turn.amount_cop) is int and result.turn.amount_cop == value


@pytest.mark.parametrize(
    "value,evidence",
    [
        ("uno", {"quote": "uno"}),
        ("1", {"quote": "uno"}),
        (True, {"quote": "uno"}),
        (1000, {"quote": "uno"}),
        (1, None),
        (1, {"quote": "1"}),
    ],
)
def test_numeric_coercion_invention_and_missing_evidence_fail_closed(model_call, value, evidence):
    _, response = model_call
    response["result"] = {"intent": "negotiate", "amount_cop": value, "amount_evidence": evidence}
    result = understand_turn("uno", ctx=CONTEXT, today=TODAY, stage="ready")
    assert result.status == "model_error" and result.turn.amount_cop is None


def test_one_installment_is_not_one_peso(model_call):
    _, response = model_call
    response["result"] = {
        "intent": "negotiate",
        "installments": 1,
        "installments_evidence": {"quote": "una"},
    }
    result = understand_turn("una cuota", ctx=CONTEXT, today=TODAY, stage="ready")
    assert result.turn.installments == 1 and result.turn.amount_cop is None


@pytest.mark.parametrize(
    "text,stage,intent,field,expected",
    [
        ("código uno uno uno uno", "awaiting_identity", "verification_code", "code_token", "1111"),
        ("código 0 0 1 1", "awaiting_identity", "verification_code", "code_token", "0011"),
        ("cédula 1.000.000.001", "awaiting_customer", "identity", "identity_token", "1000000001"),
    ],
)
def test_dictated_identifiers_preserve_exact_digits_and_checkpoint_binding(
    model_call, text, stage, intent, field, expected
):
    from agents.collections.numbers import normalize_transcript_numbers
    from agents.collections.semantic_state import bind_understanding, current_understanding

    text = normalize_transcript_numbers(text, identifiers=True)
    _, response = model_call
    response["result"] = {"intent": intent, field: 0}
    result = understand_turn(text, ctx=CONTEXT, today=TODAY, stage=stage)
    assert result.status == "classified"
    assert (result.identity_candidate or result.verification_candidate) == expected
    state = {"event_id": "voice-number", "text": text}
    state["semantic_result"] = bind_understanding(state, result)
    assert current_understanding(state) == result


@pytest.mark.parametrize(
    "text,expected",
    [
        ("uno", "1"),
        ("quiero una cuota", "quiero 1 cuota"),
        ("puedo dar doscientos mil pesos", "puedo dar 200000 pesos"),
        ("250 mil al mes", "250000 al mes"),
        ("1,5 millones", "1500000"),
        ("ciento treinta y un pesos", "131 pesos"),
        ("una persona, un asesor", "una persona, un asesor"),
        ("¿Puedo hablar con alguien?", "¿Puedo hablar con alguien?"),
    ],
)
def test_transcripts_render_spoken_numbers_as_digits(text, expected):
    from agents.collections.numbers import normalize_transcript_numbers

    assert normalize_transcript_numbers(text) == expected
    assert normalize_transcript_numbers(expected) == expected


@pytest.mark.parametrize("text", ["0011", "cero cero uno uno", "0 0 1 1", "0, 0, 1, 1"])
def test_identifier_transcription_retains_leading_zeroes(text):
    from agents.collections.numbers import normalize_transcript_numbers

    first_pass = normalize_transcript_numbers(text)
    assert normalize_transcript_numbers(first_pass, identifiers=True) == "0011"


def test_comma_separated_identifiers_do_not_discard_extra_digits():
    from agents.collections.numbers import normalize_transcript_numbers

    digits = "1, 2, 3, 4, 5, 6, 7, 8, 9"
    assert normalize_transcript_numbers(digits, identifiers=True) == "123456789"
    assert normalize_transcript_numbers(digits + ", 10", identifiers=True) == digits + ", 10"
    assert normalize_transcript_numbers(digits) == digits


@pytest.mark.parametrize(
    "text,quote,value,valid",
    [
        ("Quiero una cuota", "una cuota", 1, True),
        ("Puedo dar 1 peso", "1 peso", 1, True),
        ("Puedo dar 250 mil", "250", 250, False),
        ("Puedo dar 250 mil", "mil", 1000, False),
        ("Puedo dar 1,5 millones", "1", 1, False),
        ("Puedo dar -1", "1", 1, False),
        ("no uno sino dos", "uno sino dos", 2, False),
        ("dieciséis pesos", "dieciseis", 16, True),
        ("diecise\u0301is pesos", "dieciséis", 16, True),
        ("2-3 cuotas", "3", 3, False),
    ],
)
def test_evidence_accepts_unit_labels_but_not_cropped_expressions(text, quote, value, valid):
    from agents.collections.numbers import NumberEvidence

    evidence = NumberEvidence(quote=quote)
    if valid:
        evidence.verify(text, value)
    else:
        with pytest.raises(ValueError):
            evidence.verify(text, value)


@pytest.mark.parametrize(
    "text", ["mil gracias", "millones de gracias", "mil disculpas", "doce tres"]
)
def test_non_numeric_idioms_and_ambiguous_expressions_are_preserved(text):
    from agents.collections.numbers import normalize_transcript_numbers

    assert normalize_transcript_numbers(text) == text


def test_bare_thousand_remains_a_valid_amount_and_digit_sequences_remain_separate():
    from agents.collections.numbers import normalize_transcript_numbers

    assert normalize_transcript_numbers("mil") == "1000"
    assert normalize_transcript_numbers("cero cero uno") == "0 0 1"


def test_negated_full_payment_cannot_infer_installments_without_evidence(model_call):
    _, response = model_call
    response["result"] = {"intent": "promise", "installments": 1, "frequency": "one_off"}
    result = understand_turn("No quiero pagar todo", ctx=CONTEXT, today=TODAY, stage="ready")
    assert result.status == "model_error"


@pytest.mark.parametrize(
    "text,today,expected,clarification",
    [
        ("el viernes", date(2026, 10, 19), date(2026, 10, 23), None),
        ("el viernes", date(2026, 10, 23), None, "weekday"),
        ("este viernes", date(2026, 10, 23), date(2026, 10, 23), None),
        ("el próximo viernes", date(2026, 10, 23), date(2026, 10, 30), None),
        ("el viernes que viene", date(2026, 10, 23), date(2026, 10, 30), None),
        ("el 20", date(2026, 10, 19), date(2026, 10, 20), None),
        ("el veinte", date(2026, 10, 19), date(2026, 10, 20), None),
        ("20", date(2026, 10, 19), date(2026, 10, 20), None),
        ("el 2", date(2026, 12, 30), date(2027, 1, 2), None),
        ("el 20", date(2026, 10, 25), None, "month"),
        ("20 de noviembre", date(2026, 10, 25), None, "year"),
        ("31 de febrero", date(2026, 2, 24), None, "invalid"),
        ("29 de febrero", date(2028, 2, 24), date(2028, 2, 29), None),
        ("29 de febrero de 2026", date(2026, 2, 24), None, "invalid"),
        ("el 32", date(2026, 10, 19), None, "invalid"),
        ("el viernes o el 20", date(2026, 10, 19), None, "multiple"),
        ("el 20 o 21", date(2026, 10, 19), None, "multiple"),
        ("no el viernes", date(2026, 10, 19), None, "multiple"),
    ],
)
def test_partial_dates_override_model_guesses_and_never_become_money(
    model_call, text, today, expected, clarification
):
    calls, response = model_call
    response["result"] = {
        "intent": "unclear",
        "payment_date": "2030-01-01",
        "amount_cop": 20,
        "amount_evidence": None,
    }
    result = understand_turn(
        text,
        ctx=CONTEXT,
        today=today,
        stage="awaiting_terms",
        context={
            "identity_verified": True,
            "draft_terms": {"route": "negotiation", "installments": 3},
        },
    )
    assert result.status == "classified"
    assert len(calls) == 1
    assert result.turn.payment_date == expected
    assert result.turn.date_clarification == clarification
    assert result.turn.amount_cop is None
    assert result.turn.acceptance == "none"


def test_date_components_invented_by_model_are_ignored(model_call):
    _, response = model_call
    response["result"] = {
        "intent": "unclear",
        "payment_date": "2027-12-20",
        "date_reference": {"quote": "el 20", "day": 20, "month": 12, "year": 2027},
    }
    result = understand_turn("el 20", ctx=CONTEXT, today=date(2026, 10, 19), stage="awaiting_terms")
    assert result.turn.payment_date == date(2026, 10, 20)
    assert result.turn.date_reference.month is None and result.turn.date_reference.year is None


@pytest.mark.parametrize("text", ["pago 600000 el viernes", "pago 600000 pesos el viernes"])
def test_date_does_not_discard_a_separate_full_payment_amount(text):
    result = understand_turn(
        text, ctx=SimpleNamespace(provider="mock"), today=date(2026, 10, 19), stage="ready"
    )
    assert result.turn.amount_cop == 600000
    assert result.turn.payment_date == date(2026, 10, 23)


def test_bare_number_is_not_a_date_when_installment_choice_is_pending(model_call):
    _, response = model_call
    response["result"] = {
        "intent": "negotiate",
        "installments": 1,
        "installments_evidence": {"quote": "1"},
    }
    result = understand_turn(
        "1", ctx=CONTEXT, today=TODAY, stage="ready", context={"dialogue": {"awaiting": "option"}}
    )
    assert result.turn.installments == 1 and result.turn.payment_date is None


@pytest.mark.parametrize(
    "text,fields,today,expected",
    [
        (
            "quiero 2 cuotas el 2",
            {"installments": 2, "installments_evidence": {"quote": "2"}},
            date(2026, 12, 30),
            date(2027, 1, 2),
        ),
        (
            "puedo 20 pesos al mes el 20",
            {"amount_cop": 20, "amount_evidence": {"quote": "20"}, "amount_is_capacity": True},
            date(2026, 10, 19),
            date(2026, 10, 20),
        ),
    ],
)
def test_repeated_number_keeps_distinct_financial_and_date_roles(
    model_call, text, fields, today, expected
):
    _, response = model_call
    response["result"] = {"intent": "negotiate", **fields}
    result = understand_turn(text, ctx=CONTEXT, today=today, stage="ready")
    assert result.status == "classified"
    assert result.turn.payment_date == expected
    for key, value in fields.items():
        if not key.endswith("_evidence"):
            assert getattr(result.turn, key) == value


@pytest.mark.parametrize("text", ["el 20 del próximo mes", "el viernes de la próxima semana"])
def test_unresolved_calendar_qualifier_requires_clarification(model_call, text):
    _, response = model_call
    response["result"] = {"intent": "unclear", "payment_date": "2026-10-20"}
    result = understand_turn(text, ctx=CONTEXT, today=date(2026, 10, 19), stage="awaiting_terms")
    assert result.turn.payment_date is None and result.turn.date_clarification == "multiple"


def test_old_date_clarification_does_not_capture_a_new_installment_choice(model_call):
    _, response = model_call
    response["result"] = {
        "intent": "negotiate",
        "installments": 3,
        "installments_evidence": {"quote": "3"},
    }
    result = understand_turn(
        "3",
        ctx=CONTEXT,
        today=TODAY,
        stage="ready",
        context={"dialogue": {"awaiting": "option", "pending_date": {"quote": "el 20", "day": 20}}},
    )
    assert result.turn.installments == 3 and result.turn.payment_date is None


@pytest.mark.parametrize(
    "text,pending,today,expected,question",
    [
        (
            "el próximo",
            {"quote": "el viernes", "weekday": 4},
            date(2026, 10, 23),
            date(2026, 10, 30),
            None,
        ),
        ("octubre", {"quote": "el 20", "day": 20}, date(2026, 10, 26), None, "year"),
    ],
)
def test_pending_date_answer_overrides_process_clarification_classification(
    model_call, text, pending, today, expected, question
):
    _, response = model_call
    response["result"] = {"intent": "clarify_process"}
    result = understand_turn(
        text,
        ctx=CONTEXT,
        today=today,
        stage="awaiting_terms",
        context={
            "draft_terms": {"route": "negotiation", "installments": 3},
            "dialogue": {"awaiting": "date", "pending_date": pending},
        },
    )
    assert result.turn.intent == "unclear"
    assert result.turn.payment_date == expected
    assert result.turn.date_clarification == question


def test_date_only_answer_cannot_replace_selected_plan_with_model_full_payment(model_call):
    _, response = model_call
    response["result"] = {
        "intent": "promise",
        "installments": 1,
        "frequency": "one_off",
        "payment_date": "2026-10-23",
    }
    result = understand_turn(
        "el viernes",
        ctx=CONTEXT,
        today=date(2026, 10, 19),
        stage="awaiting_terms",
        context={
            "draft_terms": {"route": "negotiation", "installments": 3},
            "dialogue": {"awaiting": "date"},
        },
    )
    assert result.turn.intent == "unclear" and result.turn.installments is None
    assert result.turn.frequency == "unspecified"


def test_spelled_day_does_not_leak_into_amount_when_request_also_selects_installments(model_call):
    _, response = model_call
    response["result"] = {
        "intent": "negotiate",
        "installments": 3,
        "installments_evidence": {"quote": "tres"},
        "amount_cop": 20,
        "amount_evidence": {"quote": "veinte"},
    }
    result = understand_turn(
        "quiero tres cuotas el veinte", ctx=CONTEXT, today=date(2026, 10, 19), stage="ready"
    )
    assert result.status == "classified" and result.turn.amount_cop is None
    assert result.turn.installments == 3 and result.turn.payment_date == date(2026, 10, 20)


def test_confirmation_during_date_question_cannot_reuse_model_supplied_old_date(model_call):
    _, response = model_call
    response["result"] = {
        "intent": "accept_offer",
        "acceptance": "unconditional_current_offer",
        "payment_date": "2026-10-23",
        "date_reference": {"quote": "el viernes", "weekday": 4},
    }
    result = understand_turn(
        "confirmo",
        ctx=CONTEXT,
        today=date(2026, 10, 23),
        stage="awaiting_terms",
        context={
            "draft_terms": {"route": "negotiation", "installments": 3},
            "dialogue": {"awaiting": "date", "pending_date": {"quote": "el viernes", "weekday": 4}},
        },
    )
    assert result.status == "classified"
    assert result.turn.payment_date is None and result.turn.date_clarification == "weekday"
    assert result.turn.acceptance == "none" and result.turn.intent == "unclear"
