"""Flujos reales del grafo con SQLite temporal y sin servicios externos."""

import json
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
from pydantic import ValidationError

from agents.collections.agent import agent
from agents.collections.memory import ConflictError, SQLiteRepository
from agents.collections.runtime import AppContext
from collections_lab.channels.contracts import TurnRequest
from collections_lab.service import ConversationService

NOW = datetime.fromisoformat("2026-10-19T09:00:00-05:00")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")

    def fail_network(*args, **kwargs):
        raise AssertionError("Las pruebas de integración deben ejecutarse sin red.")

    monkeypatch.setattr(socket.socket, "connect", fail_network)


@pytest.fixture
def service(tmp_path):
    return ConversationService(AppContext(SQLiteRepository(tmp_path / "demo.sqlite"), NOW))


def turn(service, text="", event_id="start", session_id="session-a", account_id="demo-001"):
    return service.respond(
        TurnRequest(
            session_id=session_id,
            event_id=event_id,
            account_id=account_id,
            text=text,
        )
    )


def verify(service):
    turn(service)
    reply = turn(service, "verificar 1111", "identity")
    assert reply["stage"] == "ready"
    return reply


def propose(service, text="quiero 3 cuotas desde 2026-10-20"):
    verify(service)
    reply = turn(service, text, "offer")
    assert reply["stage"] == "awaiting_confirmation"
    assert reply["offer_id"]
    assert service.context.repository.agreements() == []
    return reply


def test_identity_blocks_balance_and_offer_creation(service):
    reply = turn(service)
    assert reply["stage"] == "awaiting_identity"
    for event_id, text in [("ask", "quiero pagar el saldo"), ("bad-code", "verificar 0000")]:
        reply = turn(service, text, event_id)
        assert "600" not in reply["text"]
        assert reply["offer_id"] is None
        assert reply["agreement_id"] is None
    assert not service.context.repository.get_session("session-a")["identity_verified"]
    assert service.context.repository.agreements() == []


def test_three_identity_failures_close_session(service):
    turn(service)
    for index in range(3):
        reply = turn(service, "verificar 0000", f"bad-{index}")
    assert reply["stage"] == "closed"
    assert reply["finished"]
    later = turn(service, "verificar 1111", "late-identity")
    assert later["stage"] == "closed"
    assert "600" not in later["text"]


def test_input_cannot_assert_identity_or_terms():
    with pytest.raises(ValidationError):
        TurnRequest(session_id="a", event_id="b", identity_verified=True)


def test_three_installment_offer_requires_confirmation(service):
    reply = propose(service)
    repo = service.context.repository
    offer = repo.get_offer(reply["offer_id"], "session-a")
    assert [item.amount_cop for item in offer.installments] == [200000] * 3
    assert [item.due_date.isoformat() for item in offer.installments] == [
        "2026-10-20",
        "2026-11-20",
        "2026-12-20",
    ]
    reply = turn(service, "confirmo", "accept")
    assert reply["stage"] == "agreed"
    assert reply["finished"]
    (record,) = repo.agreements()
    assert reply["agreement_id"] == record["agreement_id"]
    assert record["status"] == "promised"
    assert record["paid"] is False
    assert repo.get_account("demo-001").balance_cop == 600000


def test_complete_payment_uses_one_installment(service):
    offered = propose(service, "pago completo el 2026-10-20")
    offer = service.context.repository.get_offer(offered["offer_id"], "session-a")
    assert len(offer.installments) == 1
    assert offer.installments[0].amount_cop == 600000
    assert turn(service, "confirmo", "accept")["stage"] == "agreed"


@pytest.mark.parametrize(
    "text",
    [
        "no acepto",
        "confirmo si me descuentan cien mil",
        "sí, pero solo puedo pagar cien mil",
    ],
)
def test_rejection_or_conditional_acceptance_never_creates_agreement(service, text):
    propose(service)
    reply = turn(service, text, "conditional")
    assert reply["agreement_id"] is None
    assert service.context.repository.agreements() == []


def test_accepting_without_offer_does_not_create_agreement(service):
    verify(service)
    reply = turn(service, "confirmo", "premature")
    assert reply["agreement_id"] is None
    assert service.context.repository.agreements() == []


def test_unsupported_installment_count_requires_human_review(service):
    verify(service)
    reply = turn(service, "quiero 12 cuotas desde 2026-10-20", "unsupported")
    assert reply["stage"] == "handoff"
    assert reply["offer_id"] is None
    assert service.context.repository.agreements() == []


def test_resume_with_new_repository_and_service_preserves_pending_offer(service):
    offer = propose(service)
    fresh = ConversationService(AppContext(SQLiteRepository(service.context.repository.path), NOW))
    reply = turn(fresh, "confirmo", "accept")
    assert reply["stage"] == "agreed"
    assert reply["offer_id"] == offer["offer_id"]
    assert len(fresh.context.repository.agreements()) == 1


def test_exact_event_replay_is_idempotent_and_conflicting_event_is_rejected(service):
    propose(service)
    first = turn(service, "confirmo", "accept")
    revision = service.context.repository.get_session("session-a")["revision"]
    assert turn(service, "confirmo", "accept") == first
    assert service.context.repository.get_session("session-a")["revision"] == revision
    assert len(service.context.repository.agreements()) == 1
    with pytest.raises(ConflictError):
        turn(service, "terminar", "accept")


def test_balance_change_invalidates_pending_offer(service):
    propose(service)
    repo = service.context.repository
    account = repo.get_account("demo-001").model_dump(mode="json")
    # Incluso un backend defectuoso que no incremente versión debe detectarse.
    account["balance_cop"] = 400000
    with repo.connection() as db:
        db.execute("UPDATE accounts SET data=? WHERE id=?", (json.dumps(account), "demo-001"))
    reply = turn(service, "confirmo", "accept")
    assert reply["stage"] == "ready"
    assert reply["offer_id"] is None
    assert repo.agreements() == []


def test_reported_payment_does_not_mark_debt_as_paid(service):
    verify(service)
    reply = turn(service, "ya pagué", "reported")
    assert reply["stage"] == "handoff"
    repo = service.context.repository
    assert repo.get_session("session-a")["handoff_reason"] == "reported_payment"
    assert repo.get_account("demo-001").balance_cop == 600000
    assert repo.agreements() == []


def test_dispute_stops_negotiation(service):
    propose(service)
    reply = turn(service, "no reconozco esa deuda", "dispute")
    assert reply["stage"] == "handoff"
    assert reply["offer_id"] is None
    assert turn(service, "confirmo", "late-accept")["stage"] == "handoff"
    assert service.context.repository.agreements() == []


def test_explicit_close_prevents_later_acceptance(service):
    propose(service)
    reply = turn(service, "terminar", "end")
    assert reply["stage"] == "closed"
    assert reply["offer_id"] is None
    assert turn(service, "confirmo", "late-accept")["stage"] == "closed"
    assert service.context.repository.agreements() == []


def test_new_proposal_replaces_previous_offer(service):
    old = propose(service)
    new = turn(service, "quiero 2 cuotas desde 2026-10-21", "replacement")
    assert new["stage"] == "awaiting_confirmation"
    assert new["offer_id"] != old["offer_id"]
    turn(service, "confirmo", "accept")
    (record,) = service.context.repository.agreements()
    assert record["offer"]["offer_id"] == new["offer_id"]
    assert [item["amount_cop"] for item in record["offer"]["installments"]] == [300000] * 2


def test_invalid_new_proposal_cannot_accept_previous_offer(service):
    propose(service)
    reply = turn(service, "quiero 2 cuotas desde 2026-12-01", "invalid-replacement")
    assert reply["offer_id"] is None
    turn(service, "confirmo", "accept")
    assert service.context.repository.agreements() == []


def test_switching_from_installments_to_full_payment_resets_installment_count(service):
    propose(service)
    reply = turn(service, "pago completo el 2026-10-21", "full-payment")
    assert reply["stage"] == "awaiting_confirmation"
    offer = service.context.repository.get_offer(reply["offer_id"], "session-a")
    assert len(offer.installments) == 1
    assert offer.installments[0].amount_cop == 600000


def test_missing_date_can_be_supplied_on_following_turn(service):
    verify(service)
    reply = turn(service, "quiero 3 cuotas", "partial")
    assert reply["stage"] == "awaiting_terms"
    assert reply["offer_id"] is None
    reply = turn(service, "2026-10-20", "date")
    assert reply["stage"] == "awaiting_confirmation"
    assert reply["offer_id"]


def test_recording_failure_rolls_back_agreement_session_and_event(service, monkeypatch):
    propose(service)
    repo = service.context.repository
    original_session = repo.get_session("session-a")
    original_save = repo.save_turn

    def fail_after_save(state, reply):
        original_save(state, reply)
        raise RuntimeError("Simulación de fallo al persistir el turno")

    monkeypatch.setattr(repo, "save_turn", fail_after_save)
    with pytest.raises(RuntimeError, match="persistir"):
        turn(service, "confirmo", "accept")
    assert repo.agreements() == []
    assert repo.get_session("session-a") == original_session
    with repo.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE event_id='accept'").fetchone()[0] == 0
    monkeypatch.setattr(repo, "save_turn", original_save)
    assert turn(service, "confirmo", "accept")["stage"] == "agreed"
    assert len(repo.agreements()) == 1


def test_failed_first_turn_does_not_leave_contact_or_session(service, monkeypatch):
    repo = service.context.repository

    def fail_save(state, reply):
        raise RuntimeError("Fallo antes de guardar")

    monkeypatch.setattr(repo, "save_turn", fail_save)
    with pytest.raises(RuntimeError):
        turn(service)
    assert repo.get_session("session-a") is None
    assert repo.contacts_for("demo-001") == []


def test_same_account_cannot_be_contacted_again_same_day(service):
    turn(service)
    reply = turn(service, session_id="another-session")
    assert reply["stage"] == "closed"
    assert "600" not in reply["text"]


def test_session_cannot_change_account(service):
    verify(service)
    with pytest.raises(ValueError, match="cambiar de cuenta"):
        turn(service, "confirmo", "swapped", account_id="demo-002")
    assert service.context.repository.get_session("session-a")["account_id"] == "demo-001"


def test_concurrent_retries_across_repository_instances_create_one_agreement(service):
    propose(service)
    second = ConversationService(AppContext(SQLiteRepository(service.context.repository.path), NOW))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(turn, service, "confirmo", "accept")
        second_future = pool.submit(turn, second, "confirmo", "accept")
        first = first_future.result(timeout=10)
        second_reply = second_future.result(timeout=10)
    assert first == second_reply
    assert first["stage"] == "agreed"
    assert len(service.context.repository.agreements()) == 1


def test_raw_studio_invocation_does_not_keep_agreement_when_save_turn_fails(service, monkeypatch):
    propose(service)
    repo = service.context.repository
    original_session = repo.get_session("session-a")
    original_save = repo.save_turn

    def fail_save(state, reply):
        raise RuntimeError("Fallo de persistencia en Studio")

    monkeypatch.setattr(repo, "save_turn", fail_save)
    request = TurnRequest(session_id="session-a", event_id="studio-accept", text="confirmo")
    with pytest.raises(RuntimeError, match="Studio"):
        agent.invoke(request.model_dump(), context=service.context)
    assert repo.agreements() == []
    assert repo.get_session("session-a") == original_session
    with repo.connection() as db:
        count = db.execute("SELECT COUNT(*) FROM events WHERE event_id='studio-accept'").fetchone()[
            0
        ]
    assert count == 0
    monkeypatch.setattr(repo, "save_turn", original_save)
    result = agent.invoke(request.model_dump(), context=service.context)
    assert result["reply"]["stage"] == "agreed"
    assert len(repo.agreements()) == 1


def test_conditional_acceptance_invalidates_offer_before_following_yes(service):
    propose(service)
    conditional = turn(service, "acepto si me descuentan", "conditional")
    assert conditional["offer_id"] is None
    assert conditional["stage"] != "awaiting_confirmation"
    reply = turn(service, "sí", "followup-yes")
    assert reply["agreement_id"] is None
    assert reply["offer_id"] is None
    assert service.context.repository.agreements() == []
    # La negociación solo se retoma después de presentar términos concretos nuevamente.
    renewed = turn(service, "quiero 2 cuotas desde 2026-10-21", "renewed-offer")
    assert renewed["stage"] == "awaiting_confirmation"
    assert turn(service, "confirmo", "renewed-accept")["stage"] == "agreed"


def test_unauthorized_contact_blocks_start_without_balance_or_contact_record(service, monkeypatch):
    repo = service.context.repository
    monkeypatch.setattr(repo, "is_contact_authorized", lambda account_id: False)
    reply = turn(service)
    assert reply["stage"] == "closed"
    assert reply["finished"] is True
    assert reply["offer_id"] is None
    assert "600" not in reply["text"]
    assert repo.contacts_for("demo-001") == []
    assert repo.agreements() == []
