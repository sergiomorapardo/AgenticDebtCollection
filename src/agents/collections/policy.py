"""Cálculo determinista de ofertas; nunca delega importes ni fechas al modelo."""

import calendar
import hashlib
import json
from datetime import date, timedelta

from .config import data_directory
from .domain import Account, Installment, Offer, Policy


def load_account(account_id: str) -> Account:
    accounts = json.loads((data_directory() / "fixtures" / "accounts.json").read_text())
    matches = [
        Account.model_validate(item) for item in accounts if item["account_id"] == account_id
    ]
    if len(matches) != 1:
        raise ValueError("La cuenta ficticia no existe o está duplicada.")
    return matches[0]


def load_policy() -> Policy:
    return Policy.model_validate_json(
        (data_directory() / "policies" / "demo_policy.json").read_text()
    )


def _add_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    year, month = divmod(month_index, 12)
    month += 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def build_offer(
    account: Account,
    policy: Policy,
    *,
    today: date,
    first_payment_date: date,
    installments: int = 1,
) -> Offer:
    # Se revalida incluso si un llamador usó model_copy sin validación.
    account = Account.model_validate(account.model_dump())
    policy = Policy.model_validate(policy.model_dump())
    if type(installments) is not int or not 1 <= installments <= policy.max_installments:
        raise ValueError("Cantidad de cuotas fuera de la política ficticia.")
    if not today <= first_payment_date <= today + timedelta(days=policy.first_payment_within_days):
        raise ValueError("La primera fecha debe estar dentro de la ventana permitida.")
    quotient, remainder = divmod(account.balance_cop, installments)
    if quotient == 0:
        raise ValueError("El saldo no permite cuotas positivas en pesos enteros.")
    schedule = [
        Installment(
            due_date=_add_months(first_payment_date, index),
            amount_cop=quotient + (index < remainder),
        )
        for index in range(installments)
    ]
    # El identificador vincula términos, versiones y política completa. No es una firma.
    payload = {
        "account_id": account.account_id,
        "account_version": account.account_version,
        "balance_cop": account.balance_cop,
        "policy": policy.model_dump(mode="json"),
        "created_on": today.isoformat(),
        "installments": [item.model_dump(mode="json") for item in schedule],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return Offer(
        offer_id=f"offer-{digest}",
        account_id=account.account_id,
        account_version=account.account_version,
        policy_version=policy.policy_version,
        installments=schedule,
        created_on=today,
    )


def validate_offer(offer: Offer, account: Account, policy: Policy, today: date) -> None:
    """Revalida una oferta interna antes de persistir; no autoriza aceptación por sí sola."""
    offer = Offer.model_validate(offer.model_dump())
    if offer.created_on > today:
        raise ValueError("La oferta tiene una fecha de creación futura.")
    if offer.installments[0].due_date < today:
        raise ValueError("La primera fecha de pago ya venció; se requiere una nueva oferta.")
    expected = build_offer(
        account,
        policy,
        today=offer.created_on,
        first_payment_date=offer.installments[0].due_date,
        installments=len(offer.installments),
    )
    if offer != expected:
        raise ValueError("La oferta cambió o ya no corresponde al saldo y política vigentes.")
