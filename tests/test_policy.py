"""Verificación de importes, calendarios y revalidación antes de guardar."""

from datetime import date

import pytest
from pydantic import ValidationError

from agents.collections.domain import Account, Policy
from agents.collections.policy import build_offer, load_account, load_policy, validate_offer

TODAY = date(2026, 10, 20)


@pytest.fixture
def account():
    return load_account("demo-001")


@pytest.fixture
def policy():
    return load_policy()


def test_load_unknown_account_rejected():
    with pytest.raises(ValueError, match="no existe"):
        load_account("nonexistent")


def test_equal_terms_have_stable_id(account, policy):
    offer = build_offer(account, policy, today=TODAY, first_payment_date=TODAY)
    assert offer == build_offer(account, policy, today=TODAY, first_payment_date=TODAY)
    assert offer.installments[0].amount_cop == 600000
    validate_offer(offer, account, policy, TODAY)


def test_rounding_preserves_every_peso(account, policy):
    account = account.model_copy(update={"balance_cop": 600002})
    offer = build_offer(account, policy, today=TODAY, first_payment_date=TODAY, installments=3)
    assert [item.amount_cop for item in offer.installments] == [200001, 200001, 200000]
    assert sum(item.amount_cop for item in offer.installments) == account.balance_cop


@pytest.mark.parametrize("year, february_day", [(2027, 28), (2028, 29)])
def test_month_end_uses_original_day(account, policy, year, february_day):
    first = date(year, 1, 31)
    offer = build_offer(account, policy, today=first, first_payment_date=first, installments=3)
    assert [item.due_date for item in offer.installments] == [
        first,
        date(year, 2, february_day),
        date(year, 3, 31),
    ]


def test_calendar_crosses_year(account, policy):
    first = date(2026, 12, 31)
    offer = build_offer(account, policy, today=first, first_payment_date=first, installments=3)
    assert offer.installments[1].due_date == date(2027, 1, 31)
    assert offer.installments[2].due_date == date(2027, 2, 28)


@pytest.mark.parametrize("count", [0, -1, 4, True, 2.0, "2"])
def test_invalid_installment_counts_rejected(account, policy, count):
    with pytest.raises(ValueError):
        build_offer(account, policy, today=TODAY, first_payment_date=TODAY, installments=count)


@pytest.mark.parametrize("first", [date(2026, 10, 19), date(2026, 10, 28)])
def test_outside_first_payment_window_rejected(account, policy, first):
    with pytest.raises(ValueError, match="ventana"):
        build_offer(account, policy, today=TODAY, first_payment_date=first)


def test_last_day_in_window_allowed(account, policy):
    offer = build_offer(account, policy, today=TODAY, first_payment_date=date(2026, 10, 27))
    validate_offer(offer, account, policy, date(2026, 10, 27))
    with pytest.raises(ValueError, match="venció"):
        validate_offer(offer, account, policy, date(2026, 10, 28))


@pytest.mark.parametrize("change", [{"account_version": "v2"}, {"balance_cop": 599999}])
def test_changed_account_invalidates_offer(account, policy, change):
    offer = build_offer(account, policy, today=TODAY, first_payment_date=TODAY)
    with pytest.raises(ValueError, match="vigentes"):
        validate_offer(offer, account.model_copy(update=change), policy, TODAY)


@pytest.mark.parametrize("change", [{"policy_version": "v2"}, {"max_installments": 2}])
def test_changed_policy_invalidates_offer(account, policy, change):
    offer = build_offer(account, policy, today=TODAY, first_payment_date=TODAY)
    with pytest.raises(ValueError, match="vigentes"):
        validate_offer(offer, account, policy.model_copy(update=change), TODAY)


@pytest.mark.parametrize("field, value", [("amount_cop", 1), ("due_date", date(2026, 10, 21))])
def test_tampered_offer_rejected(account, policy, field, value):
    offer = build_offer(account, policy, today=TODAY, first_payment_date=TODAY)
    tampered = offer.model_copy(
        update={"installments": [offer.installments[0].model_copy(update={field: value})]}
    )
    with pytest.raises(ValueError, match="vigentes"):
        validate_offer(tampered, account, policy, TODAY)


def test_future_creation_rejected(account, policy):
    offer = build_offer(account, policy, today=TODAY, first_payment_date=TODAY)
    with pytest.raises(ValueError, match="futura"):
        validate_offer(offer, account, policy, date(2026, 10, 19))


def test_all_installments_must_be_positive(account, policy):
    account = account.model_copy(update={"balance_cop": 2})
    with pytest.raises(ValueError, match="positivas"):
        build_offer(account, policy, today=TODAY, first_payment_date=TODAY, installments=3)


def test_fractional_cop_and_unsupported_policy_rejected(account, policy):
    with pytest.raises(ValidationError):
        Account.model_validate({**account.model_dump(), "balance_cop": 10.5})
    with pytest.raises(ValidationError):
        Policy.model_validate({**policy.model_dump(), "discounts_allowed": True})
