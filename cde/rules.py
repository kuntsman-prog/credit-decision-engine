"""Декларативные правила кредитной политики.

Правила хранятся в config/rules.yaml и меняются без изменения кода, но через
ревью и версию набора правил. Условия задаются ограниченным языком
(поле, оператор, значение): никакого eval и произвольного кода, а ошибки
в правилах обнаруживаются при загрузке, а не на живой заявке.
"""
from __future__ import annotations

import operator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .models import RuleHit

OPS = {
    "eq": operator.eq, "ne": operator.ne,
    "gt": operator.gt, "gte": operator.ge,
    "lt": operator.lt, "lte": operator.le,
    "in": lambda a, b: a in b,
}
ROOTS = {"application", "bureau", "cbs", "features"}
KINDS = {"stop_factor", "refer"}


class RuleError(ValueError):
    pass


@dataclass(frozen=True)
class Condition:
    field: str
    op: str
    value: Any

    def evaluate(self, ctx: dict) -> bool | None:
        """None: данных для проверки нет (например, клиент не известен АБС)."""
        root, _, name = self.field.partition(".")
        section = ctx.get(root)
        if section is None or name not in section:
            return None
        return OPS[self.op](section[name], self.value)


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str
    reason: str
    conditions: tuple[Condition, ...]  # все условия должны выполниться

    def fires(self, ctx: dict) -> bool:
        results = [c.evaluate(ctx) for c in self.conditions]
        return all(r is True for r in results)


@dataclass(frozen=True)
class RuleSet:
    version: str
    rules: tuple[Rule, ...]

    def evaluate(self, ctx: dict) -> list[RuleHit]:
        return [RuleHit(rule_id=r.id, kind=r.kind, reason=r.reason) for r in self.rules if r.fires(ctx)]


def _condition(raw: dict, rule_id: str) -> Condition:
    field, op = raw.get("field", ""), raw.get("op", "")
    if op not in OPS:
        raise RuleError(f"{rule_id}: неизвестный оператор {op!r}")
    if field.partition(".")[0] not in ROOTS or "." not in field:
        raise RuleError(f"{rule_id}: недопустимое поле {field!r}, ожидается одно из {sorted(ROOTS)}.<имя>")
    if "value" not in raw:
        raise RuleError(f"{rule_id}: нет значения для сравнения")
    return Condition(field, op, raw["value"])


def load_rules(path: Path) -> RuleSet:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    seen = set()
    rules = []
    for r in raw["rules"]:
        rid = r["id"]
        if rid in seen:
            raise RuleError(f"{rid}: повторяющийся идентификатор правила")
        seen.add(rid)
        if r.get("kind") not in KINDS:
            raise RuleError(f"{rid}: тип правила должен быть одним из {sorted(KINDS)}")
        conds = r.get("when") or []
        if not conds:
            raise RuleError(f"{rid}: нет условий")
        rules.append(Rule(rid, r["kind"], r["reason"], tuple(_condition(c, rid) for c in conds)))
    return RuleSet(str(raw["version"]), tuple(rules))
