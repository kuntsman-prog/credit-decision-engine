"""Командная строка.

  python -m cde.cli train                 обучить champion и challenger, вывести метрики и бэктест
  python -m cde.cli decide                решения по всем демо-заявкам
  python -m cde.cli decide A-002          решение по одной заявке с подробностями
  python -m cde.cli shadow                сравнение champion и challenger в теневом режиме
  python -m cde.cli monitor               стабильность признаков (PSI) на сдвинутом потоке заявок
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from .audit import AuditLog
from .engine import ROOT, DecisionEngine, load_policy
from .integrations import FileBureau, FileCoreBanking
from .models import Application
from .monitoring import stability_report
from .scoring import ScoringModel
from .training import synthetic_portfolio, train_all

FIXTURES = ROOT / "data" / "fixtures"


def load_applications() -> list[Application]:
    return [Application(**json.loads(p.read_text(encoding="utf-8")))
            for p in sorted((FIXTURES / "applications").glob("*.json"))]


def make_engine(audit: Path | None = None, timeout_s: float = 1.0) -> DecisionEngine:
    return DecisionEngine.from_config(FileBureau(FIXTURES / "bureau.json"), FileCoreBanking(FIXTURES / "core_banking.json"),
                                      audit_path=audit, timeout_s=timeout_s)


def money(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ")


def print_decision(app: Application, d, verbose: bool) -> None:
    pd = f"PD {d.model.pd:.1%}, класс {d.model.grade}, балл {d.model.score}" if d.model else "без скоринга"
    print(f"{app.application_id}  {app.company_name:<28} {d.outcome.value:<8} "
          f"{money(d.approved_amount):>11} из {money(d.requested_amount):>11}   {pd}")
    if verbose:
        print("  признаки:", ", ".join(f"{k}={v}" for k, v in d.features.items()))
        print("  правила:", ", ".join(h.rule_id for h in d.rule_hits) or "не сработали")
        for r in d.reasons:
            print("  -", r)
        if d.model:
            for f in d.model.top_risk_factors:
                print(f"  вклад {f.feature}: +{f.contribution:.3f} к логарифму шансов дефолта")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cde")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("train")
    d = sub.add_parser("decide")
    d.add_argument("application_id", nargs="?")
    sub.add_parser("shadow")
    sub.add_parser("monitor")
    a = p.parse_args(argv)

    if a.cmd == "train":
        report = train_all(ROOT / "models", load_policy(ROOT / "config" / "policy.yaml"))
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif a.cmd == "decide":
        engine = make_engine(ROOT / "logs" / "decisions.jsonl")
        apps = load_applications()
        if a.application_id:
            apps = [x for x in apps if x.application_id == a.application_id]
            if not apps:
                print("заявка не найдена")
                return 1
        for app in apps:
            print_decision(app, engine.decide(app), verbose=bool(a.application_id))
    elif a.cmd == "shadow":
        engine = make_engine()
        for app in load_applications():
            engine.decide(app)
        rows = [r for r in engine.audit.records if r["event"] == "shadow.scored"]
        for r in rows:
            c, ch = r["champion"], r["challenger"]
            print(f"{r['application_id']}  champion {c['version']}: PD {c['pd']:.1%} класс {c['grade']}   "
                  f"challenger {ch['version']}: PD {ch['pd']:.1%} класс {ch['grade']}   "
                  f"{'совпадает' if r['grade_agrees'] else 'РАСХОЖДЕНИЕ'}")
        skipped = [r for r in engine.audit.records if r["event"] == "shadow.skipped"]
        agree = sum(r["grade_agrees"] for r in rows)
        print(f"\nдемо-заявки: совпадение классов {agree} из {len(rows)}; пропущено challenger: {len(skipped)}")
        # Теневой прогон на потоке синтетических заявок с известным исходом
        policy = load_policy(ROOT / "config" / "policy.yaml")
        champ, chall = engine.champion, engine.challenger
        x, y = synthetic_portfolio(5000, seed=11)
        feats = [{f: float(x[f][i]) for f in chall.features} for i in range(len(y))]
        def outcome(m, row):
            pd = m.predict_pd(row)
            g = next(g["grade"] for g in policy["grades"] if pd <= g["max_pd"])
            return policy["outcome_by_grade"][g]
        oc = np.array([outcome(champ, r) for r in feats])
        och = np.array([outcome(chall, r) for r in feats])
        only_ch = (och == "APPROVE") & (oc != "APPROVE")
        only_c = (oc == "APPROVE") & (och != "APPROVE")
        print(f"поток 5000 заявок: решения по классу совпадают в {np.mean(oc == och):.1%} случаев")
        print(f"  одобрила бы только challenger: {only_ch.sum()} заявок, фактический дефолт {y[only_ch].mean():.1%}")
        print(f"  одобрила бы только champion:   {only_c.sum()} заявок, фактический дефолт {y[only_c].mean():.1%}")
        print(f"  дефолт среди одобренных: champion {y[oc == 'APPROVE'].mean():.2%}, challenger {y[och == 'APPROVE'].mean():.2%}")
    elif a.cmd == "monitor":
        model = ScoringModel.load(ROOT / "models" / "champion.json")
        x, _ = synthetic_portfolio(3000, seed=7)
        # Имитация сдвига: ставки выросли, покрытие долга у новых заявителей упало на 25%
        x["dscr"] = x["dscr"] * 0.75
        rows = [{f: float(x[f][i]) for f in model.features} for i in range(3000)]
        print(json.dumps(stability_report(model, rows), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
