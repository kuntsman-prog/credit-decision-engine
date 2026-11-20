"""Скоринговая модель: вероятность дефолта, балл скоркарты, объяснение.

Модель хранится в JSON (версия, признаки, средние и стандартные отклонения,
коэффициенты логистической регрессии, метрики, распределения для мониторинга).
Логистическая регрессия выбрана сознательно: каждое решение раскладывается
на вклады признаков, а это требование к объяснимости кредитных моделей.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from .models import Factor, ModelResult

# Что означает рост риска по каждому признаку, в терминах для клиента и аналитика
RISK_REASONS = {
    "dscr": "Низкое покрытие платежей по долгу (DSCR)",
    "debt_to_ebitda": "Высокая долговая нагрузка (долг к EBITDA)",
    "ltv": "Высокая доля кредита в стоимости залога (LTV)",
    "years_in_business": "Небольшой срок работы компании",
    "delinquencies_30_12m": "Просрочки свыше 30 дней за последний год",
    "inquiries_6m": "Много запросов в бюро за полгода",
    "log_revenue": "Небольшая выручка",
}

# Шкала скоркарты: 600 баллов при шансах 50 к 1, каждые 20 баллов удваивают шансы
BASE_SCORE, BASE_ODDS, PDO = 600, 50.0, 20.0
FACTOR = PDO / math.log(2)
OFFSET = BASE_SCORE - FACTOR * math.log(BASE_ODDS)


MIN_FACTOR = 0.05  # вклады меньше этого не показываются как причины


class MissingFeature(Exception):
    pass


@dataclass
class ScoringModel:
    version: str
    features: list[str]
    means: list[float]
    stds: list[float]
    coef: list[float]
    intercept: float
    meta: dict

    @classmethod
    def load(cls, path: Path) -> "ScoringModel":
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls(d["version"], d["features"], d["means"], d["stds"], d["coef"], d["intercept"], d.get("meta", {}))

    def save(self, path: Path) -> None:
        path.write_text(json.dumps({
            "version": self.version, "features": self.features, "means": self.means,
            "stds": self.stds, "coef": self.coef, "intercept": self.intercept, "meta": self.meta,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def _z(self, x: dict[str, float]) -> list[float]:
        missing = [f for f in self.features if f not in x]
        if missing:
            raise MissingFeature(f"модели {self.version} не хватает признаков: {', '.join(missing)}")
        return [(x[f] - m) / s for f, m, s in zip(self.features, self.means, self.stds)]

    def predict_pd(self, x: dict[str, float]) -> float:
        logit = self.intercept + sum(c * z for c, z in zip(self.coef, self._z(x)))
        return 1 / (1 + math.exp(-logit))

    def score(self, x: dict[str, float], grades: list[dict], top_n: int = 3) -> ModelResult:
        z = self._z(x)
        pd = min(max(self.predict_pd(x), 1e-6), 1 - 1e-6)
        points = round(OFFSET + FACTOR * math.log((1 - pd) / pd))
        grade = next(g["grade"] for g in grades if pd <= g["max_pd"])
        # Вклад признака в логарифм шансов дефолта относительно среднего заёмщика выборки
        contribs = [
            Factor(feature=f, value=x[f], contribution=round(c * zi, 4), reason=RISK_REASONS.get(f, f))
            for f, c, zi in zip(self.features, self.coef, z)
        ]
        adverse = sorted((c for c in contribs if c.contribution > MIN_FACTOR), key=lambda c: c.contribution, reverse=True)
        return ModelResult(model_version=self.version, pd=round(pd, 5), score=points, grade=grade,
                           top_risk_factors=adverse[:top_n])
