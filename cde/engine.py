"""Движок кредитных решений.

Порядок обработки заявки:
1. Идемпотентность: повторная заявка с тем же application_id получает прежнее решение.
2. Обогащение: бюро кредитных историй (обязательно) и АБС (клиент может быть новым).
3. Признаки: финансовые коэффициенты и данные бюро.
4. Правила политики: стоп-факторы и основания для ручного рассмотрения.
5. Скоринг основной моделью (champion): PD, балл, класс, факторы риска.
6. Решение и лимит: класс по политике, правила, ограничение лимита по DSCR и залогу.
7. События: решение публикуется в шину; аудит и теневая модель (challenger) подписаны на него.

Принцип отказоустойчивости: недоступность данных или модели приводит к ручному
рассмотрению, но не к автоматическому одобрению.
"""
from __future__ import annotations

import math
from pathlib import Path

import yaml

from .audit import AuditLog, sha
from .events import APPLICATION_ENRICHED, APPLICATION_RECEIVED, DECISION_MADE, EventBus
from .features import annuity_payment, compute_features, max_amount_for_payment
from .integrations import (BureauClient, CircuitBreaker, CoreBankingClient, SourceUnavailable,
                           resilient_call)
from .models import Application, Decision, Outcome
from .rules import RuleSet, load_rules
from .scoring import MissingFeature, ScoringModel

ROOT = Path(__file__).resolve().parent.parent

MIN_LIMIT_SHARE = 0.5  # если лимит меньше половины запрошенного, решение принимает аналитик


def load_policy(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class DecisionEngine:
    def __init__(self, bureau: BureauClient, core_banking: CoreBankingClient, rules: RuleSet,
                 policy: dict, champion: ScoringModel, challenger: ScoringModel | None = None,
                 audit: AuditLog | None = None, bus: EventBus | None = None, timeout_s: float = 2.0):
        self.bureau = bureau
        self.core_banking = core_banking
        self.rules = rules
        self.policy = policy
        self.champion = champion
        self.challenger = challenger
        self.audit = audit or AuditLog(None)
        self.bus = bus or EventBus()
        self.timeout_s = timeout_s
        self.bureau_breaker = CircuitBreaker("бюро кредитных историй")
        self.cbs_breaker = CircuitBreaker("АБС")
        self.decisions: dict[str, Decision] = {}
        self.bus.subscribe(DECISION_MADE, self._audit_decision)
        if challenger is not None:
            self.bus.subscribe(DECISION_MADE, self._shadow_challenger)

    @classmethod
    def from_config(cls, bureau: BureauClient, core_banking: CoreBankingClient,
                    audit_path: Path | None = None, **kw) -> "DecisionEngine":
        return cls(bureau, core_banking,
                   rules=load_rules(ROOT / "config" / "rules.yaml"),
                   policy=load_policy(ROOT / "config" / "policy.yaml"),
                   champion=ScoringModel.load(ROOT / "models" / "champion.json"),
                   challenger=ScoringModel.load(ROOT / "models" / "challenger.json"),
                   audit=AuditLog(audit_path), **kw)

    # --- основной поток ---

    def decide(self, app: Application) -> Decision:
        if app.application_id in self.decisions:
            return self.decisions[app.application_id]
        self.bus.publish(APPLICATION_RECEIVED, {"application_id": app.application_id})

        data_issues = []
        try:
            bureau = resilient_call(lambda: self.bureau.get_report(app.tax_id), self.bureau_breaker, self.timeout_s)
        except SourceUnavailable as exc:
            bureau = None
            data_issues.append(f"Источник данных недоступен: {exc}")
        cbs_ok = True
        try:
            cbs = resilient_call(lambda: self.core_banking.get_profile(app.tax_id), self.cbs_breaker, self.timeout_s)
        except SourceUnavailable as exc:
            cbs, cbs_ok = None, False
            data_issues.append(f"Источник данных недоступен: {exc}")

        features = compute_features(app, bureau, cbs, self.policy["annual_rate"])
        ctx = {
            "application_id": app.application_id,
            "application": app.model_dump(),
            "bureau": bureau.model_dump() if bureau else None,
            # Клиент, о котором АБС ничего не знает, считается новым; при сбое АБС данных нет вовсе
            "cbs": (cbs.model_dump() if cbs else {"is_existing_client": False}) if cbs_ok else None,
            "features": features,
        }
        self.bus.publish(APPLICATION_ENRICHED, ctx)

        hits = self.rules.evaluate(ctx)
        stop = [h for h in hits if h.kind == "stop_factor"]
        refer = [h for h in hits if h.kind == "refer"]

        model_result = None
        try:
            model_result = self.champion.score(features, self.policy["grades"])
        except MissingFeature as exc:
            data_issues.append(str(exc))

        reasons: list[str] = []
        approved = 0.0
        if stop:
            outcome = Outcome.DECLINE
            reasons += [h.reason for h in stop]
        elif data_issues:
            outcome = Outcome.REFER
            reasons += data_issues
        else:
            outcome = Outcome(self.policy["outcome_by_grade"][model_result.grade])
            if outcome == Outcome.DECLINE:
                reasons.append(f"Высокая вероятность дефолта: PD {model_result.pd:.1%}, класс {model_result.grade}")
            elif outcome == Outcome.REFER:
                reasons.append(f"Пограничный риск: PD {model_result.pd:.1%}, класс {model_result.grade}")
            if refer and outcome == Outcome.APPROVE:
                outcome = Outcome.REFER
            if outcome != Outcome.DECLINE:
                reasons += [h.reason for h in refer]

        if outcome == Outcome.APPROVE:
            limit, limit_reasons = self._limit(app)
            approved = min(app.requested_amount, limit)
            if approved < app.requested_amount * MIN_LIMIT_SHARE:
                outcome = Outcome.REFER
                approved = 0.0
                reasons += limit_reasons + ["Допустимый лимит меньше половины запрошенной суммы"]
            elif approved < app.requested_amount:
                reasons += limit_reasons

        if outcome != Outcome.APPROVE and model_result:
            reasons += [f"Фактор риска: {f.reason}" for f in model_result.top_risk_factors]

        decision = Decision(
            application_id=app.application_id, outcome=outcome, approved_amount=approved,
            requested_amount=app.requested_amount, reasons=reasons, rule_hits=hits, model=model_result,
            features=features, ruleset_version=self.rules.version, data_issues=data_issues,
        )
        self.decisions[app.application_id] = decision
        self.bus.publish(DECISION_MADE, {"application_id": app.application_id, "ctx": ctx, "decision": decision})
        return decision

    def _limit(self, app: Application) -> tuple[float, list[str]]:
        p = self.policy
        max_service_annual = app.ebitda / p["min_dscr_for_limit"] - app.existing_debt_service_annual
        by_dscr = max_amount_for_payment(max_service_annual / 12, p["annual_rate"], app.term_months)
        by_collateral = app.collateral_value * p["max_ltv_for_limit"]
        limit = math.floor(min(by_dscr, by_collateral) / 1000) * 1000
        reasons = []
        if limit < app.requested_amount:
            if by_dscr <= by_collateral:
                reasons.append(f"Лимит снижен, чтобы покрытие платежей (DSCR) было не ниже {p['min_dscr_for_limit']}")
            else:
                reasons.append(f"Лимит снижен до {p['max_ltv_for_limit']:.0%} стоимости залога")
        return float(max(limit, 0)), reasons

    # --- подписчики событий ---

    def _audit_decision(self, event: dict) -> None:
        d: Decision = event["decision"]
        app = event["ctx"]["application"]
        self.audit.write({
            "event": DECISION_MADE,
            "application_id": d.application_id,
            "tax_id_hash": sha(app["tax_id"]),
            "input_hash": sha(repr(sorted(app.items()))),
            "ruleset_version": d.ruleset_version,
            "model_version": d.model.model_version if d.model else None,
            "outcome": d.outcome.value,
            "pd": d.model.pd if d.model else None,
            "approved_amount": d.approved_amount,
            "rule_hits": [h.rule_id for h in d.rule_hits],
            "data_issues": d.data_issues,
            "features": d.features,
        })

    def _shadow_challenger(self, event: dict) -> None:
        """Теневой режим: challenger оценивает ту же заявку, но на решение не влияет."""
        d: Decision = event["decision"]
        try:
            res = self.challenger.score(event["ctx"]["features"], self.policy["grades"])
        except MissingFeature as exc:
            self.audit.write({"event": "shadow.skipped", "application_id": d.application_id, "reason": str(exc)})
            return
        would_be = self.policy["outcome_by_grade"][res.grade]
        self.audit.write({
            "event": "shadow.scored",
            "application_id": d.application_id,
            "champion": {"version": d.model.model_version if d.model else None,
                         "pd": d.model.pd if d.model else None, "grade": d.model.grade if d.model else None},
            "challenger": {"version": res.model_version, "pd": res.pd, "grade": res.grade, "grade_outcome": would_be},
            "grade_agrees": bool(d.model and d.model.grade == res.grade),
        })


def annual_service_for(app: Application, rate: float) -> float:
    """Годовые платежи с учётом нового кредита: удобно для отчётов и тестов."""
    return app.existing_debt_service_annual + annuity_payment(app.requested_amount, rate, app.term_months) * 12
