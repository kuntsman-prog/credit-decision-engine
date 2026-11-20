"""Мониторинг стабильности: PSI (Population Stability Index).

PSI сравнивает распределение признака в новых заявках с обучающей выборкой.
Принятые пороги: до 0,1 стабильно, 0,1–0,25 умеренный сдвиг (наблюдать),
выше 0,25 существенный сдвиг (модель нужно перепроверить или переобучить).
"""
from __future__ import annotations

import math

import numpy as np

from .scoring import ScoringModel


def psi(expected: list[float], edges: list[float], values: list[float], eps: float = 1e-4) -> float:
    counts = np.histogram(values, bins=np.concatenate([[-np.inf], edges, [np.inf]]))[0]
    actual = counts / max(counts.sum(), 1)
    return float(sum((a - e) * math.log((a + eps) / (e + eps)) for a, e in zip(actual, expected)))


def status(value: float) -> str:
    return "стабильно" if value < 0.1 else "умеренный сдвиг" if value < 0.25 else "существенный сдвиг"


def stability_report(model: ScoringModel, rows: list[dict[str, float]]) -> dict:
    report = {}
    for f in model.features:
        values = [r[f] for r in rows if f in r]
        b = model.meta["bins"][f]
        v = psi(b["expected"], b["edges"], values)
        report[f] = {"psi": round(v, 4), "status": status(v), "n": len(values)}
    return report
