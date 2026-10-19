"""Semantic consumers use event-bound meaning, never reclassify current wording."""

from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pytest

from agents.collections import dialogue
from agents.collections.conversation import LEGACY_CLOSE
from agents.collections.memory import SQLiteRepository
from agents.collections.nodes.intake import node as intake
from agents.collections.nodes.interpret import node as interpretation
from agents.collections.policy import build_offer, load_policy
from agents.collections.runtime import AppContext
from agents.collections.semantic import TurnResult, TurnUnderstanding
from agents.collections.semantic_state import bind_understanding


@pytest.fixture
def runtime(tmp_path):
    context = AppContext(
        SQLiteRepository(tmp_path / "semantic.sqlite"),
        datetime.fromisoformat("2026-10-19T09:00:00-05:00"),
    )
    return SimpleNamespace(context=context)


def state_for(
    intent, *, text="A wording without any recognized keyword", status="classified", **fields
):
    state = {
        "session_id": "semantic-session",
        "event_id": "semantic-event",
        "account_id": "demo-001",
        "text": text,
        "session": {
            "account_id": "demo-001",
            "stage": "ready",
            "identity_verified": True,
            "pending_offer_id": None,
            "draft_terms": {},
            "dialogue": {},
            "turns": 1,
            "identity_attempts": 0,
            "clarifications": 0,
            "negotiations": 0,
            "history": [],
            "agreement_id": None,
        },
    }
    result = TurnResult(TurnUnderstanding(intent=intent, **fields), status)
    state["semantic_result"] = bind_understanding(state, result)
    return state


def apply_interpretation(state, runtime):
    return {**state, **interpretation.interpret_turn(state, runtime)}


def forbid(*args, **kwargs):
    raise AssertionError("Semantic consumers must not reclassify or parse current text")


@pytest.fixture(autouse=True)
def forbid_legacy_classification(monkeypatch):
    monkeypatch.setattr(interpretation, "parse_turn", forbid)
    monkeypatch.setattr(intake, "requests_end", forbid)
    monkeypatch.setattr(dialogue, "explicit_refusal", forbid)


def test_end_uses_meaning_even_when_text_looks_like_payment(runtime, monkeypatch):
    state = state_for("end", text="Pagaré mañana")
    monkeypatch.setattr(runtime.context.repository, "get_session", lambda _: state["session"])
    result = intake.prepare_turn(state, runtime)
    assert result["intake_route"] == "close"


def test_end_keyword_does_not_override_other_semantic_meaning(runtime, monkeypatch):
    state = state_for("complaint", text="terminar")
    monkeypatch.setattr(runtime.context.repository, "get_session", lambda _: state["session"])
    assert intake.prepare_turn(state, runtime)["intake_route"] == "interpret"


def test_model_error_does_not_attempt_identity_or_end(runtime, monkeypatch):
    state = state_for("unclear", text="terminar", status="model_error")
    state["session"].update(stage="awaiting_identity", identity_verified=False)
    monkeypatch.setattr(runtime.context.repository, "get_session", lambda _: state["session"])
    result = intake.prepare_turn(state, runtime)
    assert result["intake_route"] == "response"
    assert result["session"]["identity_attempts"] == 0
    assert result["session"]["stage"] == "awaiting_identity"


def test_verification_uses_exact_candidate_not_message_pattern(runtime, monkeypatch):
    state = state_for("verification_code", text="Mi código es 1111", code_token=0)
    state["semantic_result"]["verification_candidate"] = "1111"
    state["session"].update(stage="awaiting_identity", identity_verified=False)
    monkeypatch.setattr(runtime.context.repository, "get_session", lambda _: state["session"])
    result = intake.prepare_turn(state, runtime)
    assert result["session"]["identity_verified"] is True
    assert result["session"]["stage"] == "ready"


def test_code_looking_text_without_semantic_candidate_cannot_verify(runtime, monkeypatch):
    state = state_for("greeting", text="verificar 1111")
    state["session"].update(stage="awaiting_identity", identity_verified=False)
    monkeypatch.setattr(runtime.context.repository, "get_session", lambda _: state["session"])
    result = intake.prepare_turn(state, runtime)
    assert not result["session"]["identity_verified"]
    assert result["session"]["identity_attempts"] == 0
    assert "600" not in result["response_text"]


def test_verified_promise_uses_semantic_terms(runtime):
    state = state_for("promise", amount_cop=600000, payment_date="2026-10-20")
    result = interpretation.interpret_turn(state, runtime)
    assert result["intent_route"] == "promise"
    assert result["session"]["draft_terms"]["amount_cop"] == 600000


@pytest.mark.parametrize(
    "frequency, route, capacity",
    [
        ("monthly", "negotiation", True),
        ("one_off", "promise", False),
    ],
)
def test_frequency_answers_use_typed_frequency(runtime, frequency, route, capacity):
    state = state_for("unclear", text="A frequency paraphrase", frequency=frequency)
    state["session"]["draft_terms"] = {"amount_cop": 200000, "route": "negotiation"}
    state["session"]["dialogue"] = {"awaiting": "frequency"}
    result = interpretation.interpret_turn(state, runtime)
    assert result["intent_route"] == route
    assert result["interpretation"]["amount_cop"] == 200000
    assert result["interpretation"]["amount_is_capacity"] is capacity


def test_frequency_keyword_without_typed_frequency_does_not_change_terms(runtime):
    state = state_for("unclear", text="mensual")
    state["session"]["draft_terms"] = {"amount_cop": 200000, "route": "negotiation"}
    state["session"]["dialogue"] = {"awaiting": "frequency"}
    result = interpretation.interpret_turn(state, runtime)
    assert result["intent_route"] == "response"
    assert not result["interpretation"]["amount_is_capacity"]


@pytest.mark.parametrize(
    "fields",
    [
        {"acceptance": "none"},
        {"acceptance": "unconditional_current_offer", "amount_cop": 1},
        {"acceptance": "unconditional_current_offer", "frequency": "monthly"},
        {"acceptance": "unconditional_current_offer", "amount_is_capacity": True},
    ],
)
def test_financial_acceptance_rejects_missing_or_changed_terms(runtime, fields):
    state = state_for("accept_offer", **fields)
    state["session"].update(stage="awaiting_confirmation", pending_offer_id="old-offer")
    result = interpretation.interpret_turn(state, runtime)
    assert result["intent_route"] != "confirmation"
    assert result["session"]["pending_offer_id"] is None


def test_unconditional_acceptance_requires_current_backend_offer(runtime):
    state = state_for("accept_offer", acceptance="unconditional_current_offer")
    assert interpretation.interpret_turn(state, runtime)["intent_route"] != "confirmation"
    state["session"].update(stage="awaiting_confirmation", pending_offer_id="current-offer")
    assert interpretation.interpret_turn(state, runtime)["intent_route"] != "confirmation"
    state["semantic_result"] = bind_understanding(
        state,
        TurnResult(
            TurnUnderstanding(intent="accept_offer", acceptance="unconditional_current_offer"),
            "classified",
        ),
    )
    assert interpretation.interpret_turn(state, runtime)["intent_route"] == "confirmation"


@pytest.mark.parametrize(
    "intent, route", [("counteroffer", "negotiation"), ("request_exception", "handoff")]
)
def test_conditional_meaning_preserves_counteroffer_or_exception(runtime, intent, route):
    state = state_for(intent, acceptance="conditional", amount_cop=100000)
    result = interpretation.interpret_turn(state, runtime)
    assert result["interpretation"]["intent"] == intent
    assert result["intent_route"] == route


def test_conditional_acceptance_invalidates_pending_offer(runtime, monkeypatch):
    state = state_for("accept_offer", acceptance="conditional")
    state["session"].update(stage="awaiting_confirmation", pending_offer_id="old-offer")
    account = runtime.context.repository.get_account("demo-001")
    offer = build_offer(
        account,
        load_policy(),
        today=runtime.context.today,
        first_payment_date=runtime.context.today,
    )
    monkeypatch.setattr(runtime.context.repository, "get_offer", lambda *_: offer)
    result = interpretation.interpret_turn(state, runtime)
    assert result["interpretation"]["intent"] == "reject_offer"
    assert result["session"]["pending_offer_id"] is None


def test_options_are_computed_and_never_create_acceptance_target(runtime):
    state = state_for("options")
    state["session"].update(stage="awaiting_confirmation", pending_offer_id="old-offer")
    state = apply_interpretation(state, runtime)
    result = dialogue.plan_dialogue(state, runtime)
    assert result["session"]["pending_offer_id"] is None
    assert result["session"]["stage"] == "ready"
    assert "600,000 COP" in result["response_text"]
    assert "300,000 COP" in result["response_text"]
    assert "200,000 COP" in result["response_text"]
    assert "mensuales" in result["response_text"]
    assert "orientativos" in result["response_text"]
    assert "política" not in result["response_text"]
    with runtime.context.repository.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM agreements").fetchone()[0] == 0


def test_options_respect_policy_and_remembered_capacity(runtime, monkeypatch):
    state = state_for("options")
    state["session"]["dialogue"] = {"monthly_capacity_cop": 300000}
    monkeypatch.setattr(
        dialogue, "load_policy", lambda: load_policy().model_copy(update={"max_installments": 2})
    )
    result = dialogue.plan_dialogue(apply_interpretation(state, runtime), runtime)
    assert "300,000 COP" in result["response_text"]
    assert "600,000 COP" not in result["response_text"]
    assert "200,000 COP" not in result["response_text"]


def test_options_below_capacity_limit_go_to_review(runtime):
    state = state_for("options")
    state["session"]["dialogue"] = {"monthly_capacity_cop": 100000}
    result = dialogue.plan_dialogue(apply_interpretation(state, runtime), runtime)
    assert result["session"]["stage"] == "handoff"
    assert result["session"]["handoff_reason"] == "no_viable_plan"
    assert result["session"]["pending_offer_id"] is None
    assert "200,000" not in result["response_text"]


def test_no_capacity_uses_meaning_without_zero_keyword(runtime):
    state = state_for("no_capacity", text="Mis recursos no alcanzan para destinar algo")
    result = dialogue.plan_dialogue(apply_interpretation(state, runtime), runtime)
    assert result["session"]["stage"] == "handoff"
    assert result["session"]["handoff_reason"] == "no_capacity"


def test_zero_keyword_cannot_override_positive_semantic_capacity(runtime):
    state = state_for("counteroffer", text="nada", amount_cop=200000, amount_is_capacity=True)
    state["session"]["dialogue"] = {"awaiting": "capacity"}
    result = dialogue.plan_dialogue(apply_interpretation(state, runtime), runtime)
    assert result["session"]["stage"] != "handoff"
    assert result["session"]["dialogue"]["monthly_capacity_cop"] == 200000


def test_refusal_counter_uses_semantic_intent(runtime):
    state = state_for("refuse", text="A refusal paraphrase")
    state["session"]["dialogue"] = {"refusals": 1}
    result = dialogue.plan_dialogue(apply_interpretation(state, runtime), runtime)
    assert result["session"]["stage"] == "closed"
    assert result["session"]["close_reason"] == "repeated_refusal"


def test_legacy_resume_routes_semantic_options_to_same_turn_options(runtime, monkeypatch):
    state = state_for("options")
    state["session"].update(
        stage="closed", history=[{"user": "ayúdame con python", "assistant": LEGACY_CLOSE}]
    )
    monkeypatch.setattr(
        runtime.context.repository, "get_session", lambda _: deepcopy(state["session"])
    )
    prepared = intake.prepare_turn(state, runtime)
    assert prepared["intake_route"] == "interpret"
    result = dialogue.plan_dialogue(apply_interpretation({**state, **prepared}, runtime), runtime)
    assert "200,000 COP" in result["response_text"]
    assert result["session"]["stage"] == "ready"


def test_legitimate_closed_session_never_reopens_for_continue(runtime, monkeypatch):
    state = state_for("continue")
    state["session"].update(stage="closed", close_reason="customer_request")
    monkeypatch.setattr(
        runtime.context.repository, "get_session", lambda _: deepcopy(state["session"])
    )
    result = intake.prepare_turn(state, runtime)
    assert result["session"]["stage"] == "closed"
    assert result["intake_route"] == "response"


@pytest.mark.parametrize("intent", ["options", "continue"])
@pytest.mark.parametrize("capacity", [100000, 250000])
def test_options_apply_capacity_declared_in_same_turn(runtime, intent, capacity):
    state = state_for(
        intent,
        text=f"Qué opciones hay si solo puedo pagar {capacity} al mes",
        amount_cop=capacity,
        amount_is_capacity=True,
        frequency="monthly",
    )
    state["session"]["dialogue"] = {"monthly_capacity_cop": 600000}
    result = dialogue.plan_dialogue(apply_interpretation(state, runtime), runtime)
    assert result["session"]["dialogue"]["monthly_capacity_cop"] == capacity
    assert result["session"]["pending_offer_id"] is None
    assert "600,000 COP" not in result["response_text"]
    assert "300,000 COP" not in result["response_text"]
    if capacity == 100000:
        assert result["session"]["stage"] == "handoff"
        assert result["session"]["handoff_reason"] == "no_viable_plan"
        assert "200,000 COP" not in result["response_text"]
    else:
        assert result["session"]["stage"] == "ready"
        assert "3 cuotas mensuales" in result["response_text"]
        assert "200,000 COP" in result["response_text"]


@pytest.mark.parametrize("intent", ["promise", "negotiate", "counteroffer", "unclear"])
def test_explicit_monthly_capacity_routes_to_negotiation_even_when_labeled_promise(runtime, intent):
    state = state_for(
        intent,
        text="Puedo pagar 250 mil al mes",
        amount_cop=250000,
        amount_is_capacity=True,
        frequency="monthly",
    )
    result = interpretation.interpret_turn(state, runtime)
    assert result["intent_route"] == "negotiation"
    assert result["interpretation"]["intent"] == "counteroffer"
    assert result["session"]["draft_terms"] == {
        "route": "negotiation",
        "amount_cop": 250000,
        "amount_is_capacity": True,
    }
    assert result["session"]["dialogue"]["monthly_capacity_cop"] == 250000
    assert result["session"]["pending_offer_id"] is None


@pytest.mark.parametrize("intent", ["promise", "negotiate", "counteroffer", "unclear"])
@pytest.mark.parametrize("route", ["promise", "negotiation"])
def test_date_only_turn_completes_existing_financial_terms(runtime, intent, route):
    state = state_for(intent, text="Mañana me sirve", payment_date="2026-10-20")
    previous = {
        "route": route,
        "installments": 3 if route == "negotiation" else 1,
        "amount_cop": 250000 if route == "negotiation" else 600000,
        "amount_is_capacity": route == "negotiation",
    }
    state["session"].update(
        stage="awaiting_terms",
        draft_terms=deepcopy(previous),
        dialogue={"awaiting": "date", "monthly_capacity_cop": 250000},
    )
    result = interpretation.interpret_turn(state, runtime)
    assert result["intent_route"] == route
    assert result["session"]["draft_terms"] == {**previous, "payment_date": "2026-10-20"}
    assert result["session"]["dialogue"]["monthly_capacity_cop"] == 250000
    assert result["session"]["pending_offer_id"] is None


def test_date_only_counteroffer_invalidates_old_offer_without_losing_terms(runtime):
    state = state_for("counteroffer", text="Mejor el martes", payment_date="2026-10-20")
    state["session"].update(
        stage="awaiting_confirmation",
        pending_offer_id="old-offer",
        draft_terms={"route": "negotiation", "installments": 3, "payment_date": "2026-10-19"},
    )
    result = interpretation.interpret_turn(state, runtime)
    assert result["intent_route"] == "negotiation"
    assert result["session"]["draft_terms"] == {
        "route": "negotiation",
        "installments": 3,
        "payment_date": "2026-10-20",
    }
    assert result["session"]["pending_offer_id"] is None
    assert result["session"]["stage"] == "awaiting_terms"


@pytest.mark.parametrize(
    "intent, route",
    [
        ("end", "close"),
        ("dispute", "handoff"),
        ("request_exception", "handoff"),
    ],
)
@pytest.mark.parametrize(
    "fields",
    [
        {"amount_cop": 250000, "amount_is_capacity": True, "frequency": "monthly"},
        {"payment_date": "2026-10-20"},
    ],
)
def test_financial_fields_never_override_terminal_or_exception_intents(
    runtime, intent, route, fields
):
    state = state_for(intent, **fields)
    previous = {"route": "negotiation", "installments": 3}
    state["session"]["draft_terms"] = deepcopy(previous)
    result = interpretation.interpret_turn(state, runtime)
    assert result["interpretation"]["intent"] == intent
    assert result["intent_route"] == route
    assert result["session"]["draft_terms"] == previous


def test_conditional_acceptance_with_date_does_not_become_draft_continuation(runtime):
    state = state_for(
        "accept_offer",
        acceptance="conditional",
        payment_date="2026-10-20",
        text="Acepto si cambia la fecha",
    )
    state["session"]["draft_terms"] = {"route": "negotiation", "installments": 3}
    result = interpretation.interpret_turn(state, runtime)
    assert result["interpretation"]["intent"] == "reject_offer"
    assert result["intent_route"] != "confirmation"
    assert result["session"]["pending_offer_id"] is None


@pytest.mark.parametrize("fields", [{"frequency": "one_off"}, {"installments": 1}])
def test_explicit_one_off_payment_does_not_inherit_previous_installment_plan(runtime, fields):
    state = state_for("promise", payment_date="2026-10-20", **fields)
    state["session"]["draft_terms"] = {
        "route": "negotiation",
        "installments": 3,
        "amount_cop": 250000,
        "amount_is_capacity": True,
    }
    result = interpretation.interpret_turn(state, runtime)
    assert result["intent_route"] == "promise"
    assert result["session"]["draft_terms"].get("installments", 1) == 1
    assert "amount_is_capacity" not in result["session"]["draft_terms"]


@pytest.mark.parametrize(
    "changed",
    [
        {"pending_offer_id": "replacement-offer"},
        {"revision": 3},
        {"stage": "ready"},
        {"identity_verified": False},
        {"account_id": "demo-002"},
    ],
)
def test_acceptance_cannot_apply_to_a_changed_backend_snapshot(runtime, changed):
    state = state_for("accept_offer", acceptance="unconditional_current_offer")
    state["session"].update(
        stage="awaiting_confirmation", pending_offer_id="presented-offer", revision=2
    )
    state["semantic_result"] = bind_understanding(
        state,
        TurnResult(
            TurnUnderstanding(intent="accept_offer", acceptance="unconditional_current_offer"),
            "classified",
        ),
    )
    state["session"].update(changed)
    result = interpretation.interpret_turn(state, runtime)
    assert result["intent_route"] != "confirmation"
    assert result["interpretation"]["acceptance"] == "none"
    assert not runtime.context.repository.agreements()


def test_acceptance_cannot_move_to_a_different_session(runtime):
    state = state_for("accept_offer", acceptance="unconditional_current_offer")
    state["session"].update(stage="awaiting_confirmation", pending_offer_id="presented-offer")
    state["semantic_result"] = bind_understanding(
        state,
        TurnResult(
            TurnUnderstanding(intent="accept_offer", acceptance="unconditional_current_offer"),
            "classified",
        ),
    )
    state["session_id"] = "another-session"
    assert interpretation.interpret_turn(state, runtime)["intent_route"] != "confirmation"


def test_session_revision_conflict_rolls_back_new_agreement(runtime):
    from agents.collections.memory import ConflictError, event_hash
    from agents.collections.nodes.response.node import compose_response

    repo = runtime.context.repository
    state = state_for("accept_offer", acceptance="unconditional_current_offer")
    offer = build_offer(
        repo.get_account("demo-001"),
        load_policy(),
        today=runtime.context.today,
        first_payment_date=runtime.context.today,
    )
    state["session"].update(
        stage="awaiting_confirmation", pending_offer_id=offer.offer_id, revision=0
    )
    state.update(draft_offer=offer.model_dump(mode="json"), input_hash="presented")
    repo.save_turn(state, {"text": "Presented offer"})
    state["session"] = repo.get_session(state["session_id"])
    state.update(
        event_id="acceptance",
        draft_offer=None,
        commit_requested=True,
        input_hash=event_hash("demo-001", state["text"]),
    )
    with repo.connection() as db:
        db.execute("UPDATE sessions SET revision=revision+1 WHERE id=?", (state["session_id"],))
    with pytest.raises(ConflictError):
        compose_response(state, runtime)
    assert not repo.agreements()
    assert repo.get_session(state["session_id"])["stage"] == "awaiting_confirmation"
    assert repo.get_event(state["session_id"], "acceptance", state["input_hash"]) is None
