"""Расчёт признаков заявки: финансовые коэффициенты и данные бюро."""
from __future__ import annotations

import math

from .models import Application, BureauReport, CoreBankingProfile


def annuity_payment(amount: float, annual_rate: float, term_months: int) -> float:
    r = annual_rate / 12
    if r == 0:
        return amount / term_months
    return amount * r / (1 - (1 + r) ** -term_months)


def max_amount_for_payment(payment: float, annual_rate: float, term_months: int) -> float:
    """Обратная задача: максимальная сумма кредита при заданном ежемесячном платеже."""
    if payment <= 0:
        return 0.0
    r = annual_rate / 12
    if r == 0:
        return payment * term_months
    return payment * (1 - (1 + r) ** -term_months) / r


def compute_features(app: Application, bureau: BureauReport | None,
                     cbs: CoreBankingProfile | None, annual_rate: float) -> dict[str, float]:
    new_service = annuity_payment(app.requested_amount, annual_rate, app.term_months) * 12
    total_service = app.existing_debt_service_annual + new_service
    f = {
        # покрытие долга: во сколько раз EBITDA превышает платежи по всем кредитам за год
        "dscr": app.ebitda / total_service if total_service else 99.0,
        "debt_to_ebitda": (app.total_debt + app.requested_amount) / app.ebitda if app.ebitda > 0 else 99.0,
        "ltv": app.requested_amount / app.collateral_value if app.collateral_value else 9.99,
        "years_in_business": app.years_in_business,
        "log_revenue": math.log10(app.annual_revenue),
    }
    if bureau is not None:
        f["delinquencies_30_12m"] = float(bureau.delinquencies_30_12m)
        f["delinquencies_90_12m"] = float(bureau.delinquencies_90_12m)
        f["inquiries_6m"] = float(bureau.inquiries_6m)
    if cbs is not None:
        f["turnover_coverage"] = cbs.turnover_12m / app.requested_amount
        f["overdraft_days_12m"] = float(cbs.overdraft_days_12m)
    return {k: round(v, 4) for k, v in f.items()}
