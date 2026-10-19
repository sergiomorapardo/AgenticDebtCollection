"""Consumer adapters reuse only current-event meaning and preserve backend decisions."""

from types import SimpleNamespace

import pytest

from agents.collections.continuation import interpret_continuation
from agents.collections.conversation import terminal_response
from agents.collections.onboarding import onboarding_reply
from agents.collections.semantic import TurnResult, TurnUnderstanding
from agents.collections.semantic_state import bind_understanding, current_understanding


def state_for(intent, text="dale, revisemos", event="one"):
    state = {"event_id": event, "text": text}
    state["semantic_result"] = bind_understanding(
        state, TurnResult(TurnUnderstanding(intent=intent), "classified")
    )
    return state


@pytest.mark.parametrize("changed", [{"event_id": "two"}, {"text": "no, terminemos"}])
def test_previous_turn_understanding_is_not_reused(changed):
    state = state_for("continue")
    state.update(changed)
    assert current_understanding(state).status == "model_error"
    assert current_understanding(state).turn.intent == "unclear"
    assert interpret_continuation(state, {}, SimpleNamespace(provider="mock")) == (
        "unclear",
        "model_error",
    )


def test_onboarding_uses_semantic_intent_without_another_classifier(monkeypatch):
    from agents.collections import onboarding

    monkeypatch.setattr(onboarding, "classify_onboarding", lambda *args: pytest.fail("duplicate"))
    binding, response = onboarding_reply(
        "de una, muéstrame alternativas",
        {"account_id": None},
        SimpleNamespace(provider="mock"),
        understanding=TurnResult(TurnUnderstanding(intent="options"), "classified"),
    )
    assert binding["onboarding_intent"] == "willing"
    assert "identificación" in response["text"]
    assert not response["finished"]


def test_current_semantics_override_exact_words_in_continuation():
    state = state_for("unclear", "terminar")
    assert interpret_continuation(state, {}, SimpleNamespace(provider="mock")) == (
        "unclear",
        "classified",
    )
    state = state_for("continue", "sí, por favor muéstrame cómo podemos seguir")
    assert interpret_continuation(state, {}, SimpleNamespace(provider="mock")) == (
        "resume",
        "classified",
    )


def test_terminal_tone_uses_semantic_complaint_and_never_reopens():
    session = {"stage": "closed", "identity_verified": True, "close_reason": "customer_request"}
    assert terminal_response(session, "sin palabras", intent="complaint").startswith("Lamento")
    assert not terminal_response(session, "mal servicio", intent="continue").startswith("Lamento")
    assert session["stage"] == "closed"


def test_checkpoint_candidate_must_still_exist_in_current_message():
    state = {"event_id": "one", "text": "¿Qué código necesitas?"}
    state["semantic_result"] = bind_understanding(
        state,
        TurnResult(
            TurnUnderstanding(intent="verification_code", code_token=0),
            "classified",
            verification_candidate="1111",
        ),
    )
    result = current_understanding(state)
    assert result.status == "model_error" and result.verification_candidate is None
