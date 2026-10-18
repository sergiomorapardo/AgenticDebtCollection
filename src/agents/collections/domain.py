"""Contratos del dominio ficticio: importes enteros y términos verificables."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Account(DomainModel):
    account_id: str = Field(min_length=1)
    customer_label: str = Field(min_length=1)
    synthetic: Literal[True]
    currency: Literal["COP"]
    balance_cop: int = Field(gt=0, strict=True)
    account_version: str = Field(min_length=1)
    due_date: date


class Policy(DomainModel):
    policy_version: str = Field(min_length=1)
    synthetic: Literal[True]
    currency: Literal["COP"]
    max_installments: int = Field(ge=1, le=3, strict=True)
    first_payment_within_days: int = Field(ge=0, le=7, strict=True)
    installment_frequency: Literal["monthly"]
    additional_charges_cop: Literal[0]
    discounts_allowed: Literal[False]
    explicit_confirmation_required: Literal[True]
    exceptions_require_human: Literal[True]


class Installment(DomainModel):
    due_date: date
    amount_cop: int = Field(gt=0, strict=True)


class Offer(DomainModel):
    offer_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    account_version: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    installments: list[Installment] = Field(min_length=1, max_length=3)
    created_on: date
