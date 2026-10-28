from datetime import datetime

import pytest

from agents.collections.memory import SQLiteRepository
from collections_lab.channels.managed_agent import ManagedConversation

NOW = datetime.fromisoformat("2026-10-19T09:00:00-05:00")


@pytest.fixture
def conversation(tmp_path):
    return ManagedConversation(
        repository=SQLiteRepository(tmp_path / "db.sqlite3"),
        now=NOW,
        session_id="sesn_test",
        reviews_path=tmp_path / "reviews.jsonl",
    )


def call(conversation, name, **tool_input):
    call.counter = getattr(call, "counter", 0) + 1
    return conversation.handle(name, tool_input, f"evt_{call.counter}")


def verify(conversation, document_id="1000000001", code="1111"):
    conversation.customer_message(f"{document_id} {code}")
    return call(conversation, "collections_verify_identity", document_id=document_id, code=code)


def test_account_data_requires_identity(conversation):
    result = call(conversation, "collections_get_account_summary")
    assert "error" in result and "balance_cop" not in str(result)
    assert "error" in call(
        conversation, "collections_quote_plan", installments=1, first_payment_date="2026-10-20"
    )


def test_three_failed_codes_close_conversation(conversation):
    assert verify(conversation, code="0000") == {"verified": False, "attempts_left": 2}
    assert verify(conversation, document_id="999", code="1111") == {
        "verified": False,
        "attempts_left": 1,
    }
    assert verify(conversation, code="0000") == {"verified": False, "closed": True}
    assert "error" in verify(conversation)


def test_three_installments_saved_once(conversation):
    assert verify(conversation)["verified"] is True
    assert call(conversation, "collections_get_account_summary")["balance_cop"] == 600000
    conversation.customer_message("quiero 3 cuotas desde 2026-10-20")
    offer = call(
        conversation, "collections_quote_plan", installments=3, first_payment_date="2026-10-20"
    )
    assert [item["amount_cop"] for item in offer["installments"]] == [200000] * 3
    assert [item["due_date"] for item in offer["installments"]] == [
        "2026-10-20",
        "2026-11-20",
        "2026-12-20",
    ]
    conversation.customer_message("confirmo")
    saved = call(
        conversation,
        "collections_record_commitment",
        offer_id=offer["offer_id"],
        confirmation_quote="confirmo",
    )
    assert saved["agreement_id"].startswith("agreement-") and saved["paid"] is False
    assert len(conversation.repository.agreements()) == 1


def test_acceptance_needs_a_later_customer_turn(conversation):
    verify(conversation)
    conversation.customer_message("pago completo el 2026-10-20")
    offer = call(
        conversation, "collections_quote_plan", installments=1, first_payment_date="2026-10-20"
    )
    result = call(
        conversation,
        "collections_record_commitment",
        offer_id=offer["offer_id"],
        confirmation_quote="pago completo el 2026-10-20",
    )
    assert "quoted in this turn" in result["error"]
    assert conversation.repository.agreements() == []


def test_confirmation_quote_must_match_customer_text(conversation):
    verify(conversation)
    conversation.customer_message("quiero 2 cuotas desde 2026-10-21")
    offer = call(
        conversation, "collections_quote_plan", installments=2, first_payment_date="2026-10-21"
    )
    conversation.customer_message("acepto si me descuentan")
    result = call(
        conversation,
        "collections_record_commitment",
        offer_id=offer["offer_id"],
        confirmation_quote="acepto",
    )
    assert "latest message" in result["error"]
    assert conversation.repository.agreements() == []


def test_only_latest_offer_can_be_accepted(conversation):
    verify(conversation)
    conversation.customer_message("3 cuotas")
    old = call(
        conversation, "collections_quote_plan", installments=3, first_payment_date="2026-10-20"
    )
    conversation.customer_message("mejor 2 cuotas")
    call(conversation, "collections_quote_plan", installments=2, first_payment_date="2026-10-21")
    conversation.customer_message("confirmo")
    result = call(
        conversation,
        "collections_record_commitment",
        offer_id=old["offer_id"],
        confirmation_quote="confirmo",
    )
    assert "most recent offer" in result["error"]


def test_outside_policy_returns_limits(conversation):
    verify(conversation)
    conversation.customer_message("6 cuotas")
    result = call(
        conversation, "collections_quote_plan", installments=6, first_payment_date="2026-10-20"
    )
    assert result["error"].startswith("Outside policy") and "2026-10-26" in result["error"]


def test_review_is_queued_without_identity_data(conversation, tmp_path):
    verify(conversation)
    result = call(
        conversation,
        "collections_request_human_review",
        reason="discount_request",
        summary="Pide descuento.",
    )
    assert result["review_id"].startswith("review-")
    stored = (tmp_path / "reviews.jsonl").read_text()
    assert "1111" not in stored and "1000000001" not in stored


def test_non_ascii_and_wrong_type_inputs_return_errors(conversation):
    assert verify(conversation, code="11é1") == {"verified": False, "attempts_left": 2}
    verify(conversation)
    conversation.customer_message("cuotas")
    for bad in ("abc", True, 2.5):
        result = call(
            conversation,
            "collections_quote_plan",
            installments=bad,
            first_payment_date="2026-10-20",
        )
        assert "error" in result


def test_recording_twice_returns_same_agreement(conversation):
    verify(conversation)
    conversation.customer_message("pago completo el 2026-10-20")
    offer = call(
        conversation, "collections_quote_plan", installments=1, first_payment_date="2026-10-20"
    )
    conversation.customer_message("confirmo")
    first = call(
        conversation,
        "collections_record_commitment",
        offer_id=offer["offer_id"],
        confirmation_quote="confirmo",
    )
    second = call(
        conversation,
        "collections_record_commitment",
        offer_id=offer["offer_id"],
        confirmation_quote="confirmo",
    )
    assert first["agreement_id"] == second["agreement_id"]
    assert len(conversation.repository.agreements()) == 1


def test_second_session_same_day_is_refused_without_account_data(conversation, tmp_path):
    verify(conversation)
    again = ManagedConversation(
        repository=conversation.repository,
        now=NOW,
        session_id="sesn_other",
        reviews_path=tmp_path / "reviews.jsonl",
    )
    result = verify(again)
    assert result["verified"] is False and result["closed"] is True
    assert "error" in call(again, "collections_get_account_summary")


def test_review_summary_redacts_digit_runs(conversation, tmp_path):
    verify(conversation)
    call(
        conversation,
        "collections_request_human_review",
        reason="other",
        summary="Cliente 1000000001 con código 1111 pide plazo.",
    )
    stored = (tmp_path / "reviews.jsonl").read_text()
    assert "1000000001" not in stored and "1111" not in stored


def test_unexpected_backend_failure_still_answers(conversation, monkeypatch, caplog):
    verify(conversation)

    def broken(*args, **kwargs):
        raise RuntimeError("database locked")

    monkeypatch.setattr(conversation.repository, "get_account", broken)
    result = call(conversation, "collections_get_account_summary")
    assert result == {"error": "Internal backend error. Tell the customer to try again later."}
    assert "collections_get_account_summary" in caplog.text and "1111" not in caplog.text
