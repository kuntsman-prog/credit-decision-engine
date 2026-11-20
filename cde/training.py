"""Обучение моделей на синтетических данных и бэктест политики.

Данные синтетические: известная «истинная» зависимость дефолта от признаков
плюс шум. Это позволяет честно проверить, что модель восстанавливает
зависимости, и сравнить две версии модели без реальных клиентских данных.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

from .scoring import ScoringModel

CHAMPION_FEATURES = ["dscr", "debt_to_ebitda", "ltv", "years_in_business", "delinquencies_30_12m", "log_revenue"]
CHALLENGER_FEATURES = CHAMPION_FEATURES + ["inquiries_6m"]


def synthetic_portfolio(n: int, seed: int) -> tuple[dict[str, np.ndarray], np.ndarray]:
    rng = np.random.default_rng(seed)
    dscr = np.exp(rng.normal(0.55, 0.40, n))
    weak = (dscr < 1.2).astype(float)
    x = {
        "dscr": dscr,
        "debt_to_ebitda": np.exp(1.25 - 0.5 * np.log(dscr) + rng.normal(0, 0.45, n)),
        "ltv": np.clip(rng.beta(5, 4, n) * 1.1, 0.05, 1.2),
        "years_in_business": np.clip(rng.gamma(2.0, 4.5, n), 1.0, 40.0),
        "delinquencies_30_12m": rng.poisson(0.2 + 0.4 * weak).astype(float),
        "inquiries_6m": rng.poisson(1.5 + 2.0 * weak).astype(float),
        "log_revenue": rng.normal(6.5, 0.6, n),
    }
    logit = (-3.3 - 1.1 * (x["dscr"] - 1.7) + 0.25 * (x["debt_to_ebitda"] - 2.7) + 1.8 * (x["ltv"] - 0.6)
             - 0.06 * (x["years_in_business"] - 8) + 0.7 * x["delinquencies_30_12m"]
             + 0.18 * (x["inquiries_6m"] - 1.5) - 0.4 * (x["log_revenue"] - 6.5))
    y = (rng.random(n) < 1 / (1 + np.exp(-logit))).astype(int)
    return x, y


def ks_stat(y: np.ndarray, p: np.ndarray) -> float:
    order = np.argsort(p)
    y = y[order]
    cum_bad = np.cumsum(y) / y.sum()
    cum_good = np.cumsum(1 - y) / (1 - y).sum()
    return float(np.max(np.abs(cum_bad - cum_good)))


def _matrix(x: dict[str, np.ndarray], features: list[str]) -> np.ndarray:
    return np.column_stack([x[f] for f in features])


def train(features: list[str], version: str, x_tr, y_tr, x_te, y_te) -> ScoringModel:
    X = _matrix(x_tr, features)
    means, stds = X.mean(axis=0), X.std(axis=0)
    lr = LogisticRegression(C=1.0, max_iter=1000).fit((X - means) / stds, y_tr)
    model = ScoringModel(version, features, means.round(6).tolist(), stds.round(6).tolist(),
                         lr.coef_[0].round(6).tolist(), round(float(lr.intercept_[0]), 6), {})
    p = np.array([model.predict_pd({f: float(v) for f, v in zip(features, row)}) for row in _matrix(x_te, features)])
    auc = roc_auc_score(y_te, p)
    # Децили обучающей выборки для мониторинга стабильности (PSI)
    bins = {}
    for i, f in enumerate(features):
        edges = np.unique(np.quantile(X[:, i], np.linspace(0, 1, 11)[1:-1]))
        counts = np.histogram(X[:, i], bins=np.concatenate([[-np.inf], edges, [np.inf]]))[0]
        bins[f] = {"edges": edges.round(6).tolist(), "expected": (counts / counts.sum()).round(6).tolist()}
    model.meta = {
        "trained_at": dt.date.today().isoformat(),
        "training_rows": int(len(y_tr)), "holdout_rows": int(len(y_te)),
        "default_rate": round(float(y_tr.mean()), 4),
        "holdout_metrics": {"auc": round(auc, 4), "gini": round(2 * auc - 1, 4), "ks": round(ks_stat(y_te, p), 4)},
        "bins": bins,
        "data": "синтетический портфель, seed 42; не обучать на нём реальные решения",
    }
    return model


def backtest(model: ScoringModel, grades: list[dict], outcome_by_grade: dict, x_te, y_te) -> dict:
    X = _matrix(x_te, model.features)
    pds = np.array([model.predict_pd({f: float(v) for f, v in zip(model.features, row)}) for row in X])
    grade_of = np.array([next(g["grade"] for g in grades if p <= g["max_pd"]) for p in pds])
    approved = np.array([outcome_by_grade[g] == "APPROVE" for g in grade_of])
    return {
        "model": model.version,
        "approval_rate": round(float(approved.mean()), 4),
        "bad_rate_approved": round(float(y_te[approved].mean()), 4),
        "bad_rate_declined_or_referred": round(float(y_te[~approved].mean()), 4),
        "portfolio_bad_rate": round(float(y_te.mean()), 4),
    }


def train_all(models_dir: Path, policy: dict, n: int = 40000) -> list[dict]:
    x, y = synthetic_portfolio(n, seed=42)
    split = int(n * 0.75)
    x_tr = {k: v[:split] for k, v in x.items()}
    x_te = {k: v[split:] for k, v in x.items()}
    y_tr, y_te = y[:split], y[split:]
    models_dir.mkdir(parents=True, exist_ok=True)
    report = []
    for name, feats, version in [("champion", CHAMPION_FEATURES, "pd-corp-1.0"),
                                 ("challenger", CHALLENGER_FEATURES, "pd-corp-1.1")]:
        m = train(feats, version, x_tr, y_tr, x_te, y_te)
        m.save(models_dir / f"{name}.json")
        report.append({"role": name, "version": version, **m.meta["holdout_metrics"],
                       **backtest(m, policy["grades"], policy["outcome_by_grade"], x_te, y_te)})
    return report
