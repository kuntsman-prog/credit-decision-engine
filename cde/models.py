"""Модели данных: заявка, данные бюро и АБС, решение.

Суммы указаны в долларах США, сроки в месяцах.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Application(BaseModel):
    """Заявка юридического лица на кредит."""
    application_id: str = Field(description="Ключ идемпотентности: повторная подача возвращает прежнее решение")
    tax_id: str = Field(description="ИНН заёмщика, ключ для запросов в бюро и АБС")
    company_name: str
    industry: str
    years_in_business: float = Field(ge=0)
    annual_revenue: float = Field(gt=0)
    ebitda: float
    existing_debt_service_annual: float = Field(ge=0, description="Платежи по текущим кредитам за год")
    total_debt: float = Field(ge=0)
    requested_amount: float = Field(gt=0)
    term_months: int = Field(gt=0, le=120)
    purpose: str
    collateral_value: float = Field(ge=0)


class BureauReport(BaseModel):
    """Отчёт бюро кредитных историй."""
    active_default: bool = False
    delinquencies_30_12m: int = 0
    delinquencies_90_12m: int = 0
    active_loans: int = 0
    inquiries_6m: int = 0


class CoreBankingProfile(BaseModel):
    """Данные АБС банка о клиенте."""
    is_existing_client: bool = False
    turnover_12m: float = 0.0
    avg_balance_12m: float = 0.0
    overdraft_days_12m: int = 0


class Outcome(str, Enum):
    APPROVE = "APPROVE"
    REFER = "REFER"      # на ручное рассмотрение кредитным аналитиком
    DECLINE = "DECLINE"


class RuleHit(BaseModel):
    rule_id: str
    kind: str            # stop_factor | refer
    reason: str


class Factor(BaseModel):
    feature: str
    value: float
    contribution: float  # вклад в логарифм шансов дефолта относительно среднего заёмщика
    reason: str


class ModelResult(BaseModel):
    model_version: str
    pd: float            # вероятность дефолта на горизонте 12 месяцев
    score: int           # балл скоркарты: выше значит надёжнее
    grade: str
    top_risk_factors: list[Factor]


class Decision(BaseModel):
    application_id: str
    outcome: Outcome
    approved_amount: float
    requested_amount: float
    reasons: list[str]
    rule_hits: list[RuleHit]
    model: ModelResult | None
    features: dict[str, float]
    ruleset_version: str
    data_issues: list[str] = []
