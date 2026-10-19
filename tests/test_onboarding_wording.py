"""Verify contextual drafting, bounded retries and pre-verification authority."""

from types import SimpleNamespace

from agents.collections import onboarding_wording as wording
from agents.collections.onboarding import onboarding_reply
from agents.collections.semantic import TurnResult, TurnUnderstanding
from agents.collections.wording import ReplyDraft


def context():
    return SimpleNamespace(provider="anthropic", model="claude-haiku-4-5-20251001")


def binding():
    return {
        "account_id": None,
        "verified": False,
        "attempts": 0,
        "closed": False,
        "session_id": None,
    }


def approval(**changes):
    fields = {
        "addresses_current_message": True,
        "respects_scope_and_pause": True,
        "no_unsupported_claims": True,
        "avoids_repetition": True,
    }
    return wording.OnboardingReview(**(fields | changes))


def test_current_message_and_history_reach_writer_and_reviewer(monkeypatch):
    captured = []
    history = [{"user": "qué pasa", "assistant": "Podemos hablar de la simulación."}]

    def request(ctx, schema, system, payload):
        captured.append(payload.copy())
        return (
            ReplyDraft(
                text="Veo que estás jugando con una función. Aquí puedo orientarte sobre la simulación de cobranza."
            )
            if schema is ReplyDraft
            else approval()
        )

    monkeypatch.setattr(wording, "_request", request)
    text, status, attempts = wording.contextual_reply(
        context(), "def loca() -> you", history, "Explicar el alcance", False
    )
    assert status == "validated" and attempts == 1
    assert "función" in text
    assert all(item["recent_dialogue"] == history for item in captured)
    assert all(item["current_message"] == "def loca() -> you" for item in captured)
    assert all("account_id" not in item for item in captured)


def test_reviewer_rejection_retries_then_uses_reference(monkeypatch):
    calls = []

    def request(ctx, schema, system, payload):
        calls.append(schema)
        return (
            ReplyDraft(text="Dame tus datos para continuar.")
            if schema is ReplyDraft
            else approval(respects_scope_and_pause=False)
        )

    monkeypatch.setattr(wording, "_request", request)
    result = wording.contextual_reply(context(), "no quiero", [], "No insistiré.", True)
    assert result == ("No insistiré.", "rejected", 2)
    assert calls == [ReplyDraft, wording.OnboardingReview] * 2


def test_numeric_claims_and_exact_repetitions_never_reach_customer(monkeypatch):
    drafts = iter(["Debes pagar 500 pesos.", "Ya te expliqué la simulación."])

    def request(ctx, schema, system, payload):
        assert schema is ReplyDraft
        return ReplyDraft(text=next(drafts))

    monkeypatch.setattr(wording, "_request", request)
    history = [{"user": "hola", "assistant": "Ya te expliqué la simulación."}]
    assert wording.contextual_reply(context(), "y qué", history, "Referencia segura.", False) == (
        "Referencia segura.",
        "rejected",
        2,
    )


def test_writer_error_has_no_retry_or_business_effect(monkeypatch):
    def fail(*args):
        raise TimeoutError

    monkeypatch.setattr(wording, "_request", fail)
    assert wording.contextual_reply(context(), "hola", [], "Referencia segura.", False) == (
        "Referencia segura.",
        "model_error",
        1,
    )


def test_followups_do_not_repeat_identity_step_and_history_is_bounded(monkeypatch):
    from agents.collections import onboarding

    def generate(ctx, text, history, reference, paused, **kwargs):
        return "Esta es una simulación de cobranza.", "validated", 1

    monkeypatch.setattr(onboarding, "contextual_reply", generate)
    current = binding()
    current, first = onboarding_reply(
        "que pasa loca",
        current,
        context(),
        understanding=TurnResult(TurnUnderstanding(intent="greeting"), "classified"),
    )
    assert "1000000001" not in first["text"]
    assert "número de identificación" in first["text"]
    for text in [
        "y que pasa?",
        "def loca() -> you",
        "y eso",
        "explicame",
        "mi correo fake@example.test y 987654321",
    ]:
        current, result = onboarding_reply(
            text,
            current,
            context(),
            understanding=TurnResult(TurnUnderstanding(intent="scope"), "classified"),
        )
        assert "1000000001" not in result["text"]
    assert len(current["onboarding_history"]) == 4
    assert "fake@example.test" not in str(current["onboarding_history"])
    assert "987654321" not in str(current["onboarding_history"])
    assert not current["verified"] and current["account_id"] is None
    assert current["attempts"] == 0 and not current["closed"]


def test_scope_help_does_not_resume_pause_or_request_identity(monkeypatch):
    from agents.collections import onboarding

    captured = []

    def explain(ctx, text, history, reference, paused, **kwargs):
        captured.append(kwargs)
        assert paused
        return "La verificación protege el acceso a tu cuenta. Seguimos en pausa.", "validated", 1

    monkeypatch.setattr(onboarding, "contextual_reply", explain)
    current = binding() | {"onboarding_paused": True, "onboarding_intent": "decline"}
    understanding = TurnResult(TurnUnderstanding(intent="scope"), "classified")
    current, response = onboarding_reply(
        "¿Cuál es el dato de identificación que necesitas?",
        current,
        context(),
        understanding=understanding,
    )
    assert "verificación" in response["text"]
    assert "1000000001" not in response["text"]
    assert current["onboarding_paused"] and not current["verified"]
    assert current["account_id"] is None
    current["account_id"] = "demo-001"
    current, response = onboarding_reply(
        "¿Cómo funciona la verificación?",
        current,
        context(),
        understanding=understanding,
    )
    assert "compárteme" not in response["text"]
    assert current["attempts"] == 0 and not current["verified"]
    assert all(payload["next_step"] is None for payload in captured)


def test_optional_identity_step_is_authorized_only_when_not_paused(monkeypatch):
    captured = []

    def request(ctx, schema, system, payload):
        captured.append(payload)
        if schema is wording.ReplyDraft:
            return schema(text="¿Cuál de los números deseas usar para consultar tu cuenta?")
        return schema(
            addresses_current_message=True,
            respects_scope_and_pause=True,
            no_unsupported_claims=True,
            avoids_repetition=True,
        )

    monkeypatch.setattr(wording, "_request", request)
    for paused in (False, True):
        wording.contextual_reply(
            context(),
            "123456789 o 1000000001",
            [],
            "Aclaremos tu solicitud.",
            paused,
            intent="unclear",
            stage="awaiting_customer",
            next_step="Comparte tu identificación.",
        )
        assert captured[-1]["next_step"] == (None if paused else "Comparte tu identificación.")
        assert "123456789" not in str(captured[-1])


def test_reviewed_identity_step_has_one_owner(monkeypatch):
    from agents.collections import onboarding

    for intent in ("greeting", "continue", "options"):

        def generate(*args, **kwargs):
            assert kwargs["next_step"] is None
            return "Claro, te ayudo.", "validated", 1

        monkeypatch.setattr(onboarding, "contextual_reply", generate)
        _, response = onboarding_reply(
            "Quiero consultar",
            binding(),
            context(),
            understanding=TurnResult(TurnUnderstanding(intent=intent), "classified"),
        )
        assert response["text"].count("¿me compartes tu número de identificación?") == 1


def test_missing_or_failed_meaning_cannot_change_onboarding_business_state(monkeypatch):
    from agents.collections import onboarding

    def no_reclassification(*args, **kwargs):
        raise AssertionError("Onboarding must not classify or draft missing meaning")

    monkeypatch.setattr(onboarding, "classify_onboarding", no_reclassification)
    monkeypatch.setattr(onboarding, "contextual_reply", no_reclassification)
    for understanding in (None, TurnResult(TurnUnderstanding(intent="decline"), "model_error")):
        current, response = onboarding_reply(
            "No quiero seguir",
            binding(),
            context(),
            understanding=understanding,
        )
        assert response["interpretation_status"] == "model_error"
        assert not current.get("onboarding_paused")
        assert not current["verified"] and current["account_id"] is None
        assert current["attempts"] == 0 and not current["closed"]
