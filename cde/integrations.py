"""Интеграции с бюро кредитных историй и АБС: порты и адаптеры.

Движок зависит только от интерфейсов BureauClient и CoreBankingClient.
В демо адаптеры читают данные из JSON и умеют имитировать задержку и сбой,
в промышленном варианте их заменяют HTTP-клиенты конкретного бюро и АБС.

Устойчивость:
- таймаут на каждый вызов и ограниченное число повторов;
- автоматический выключатель (circuit breaker): после серии сбоев внешний
  сервис временно не вызывается, заявки уходят на ручное рассмотрение,
  а не ждут таймаута и не нагружают упавший сервис;
- сбой источника данных никогда не приводит к автоматическому одобрению.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Callable, Protocol, TypeVar

from .models import BureauReport, CoreBankingProfile

T = TypeVar("T")


class SourceUnavailable(Exception):
    """Источник данных не ответил или отказал."""


class BureauClient(Protocol):
    def get_report(self, tax_id: str) -> BureauReport: ...


class CoreBankingClient(Protocol):
    def get_profile(self, tax_id: str) -> CoreBankingProfile | None: ...


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 3, cooldown_s: float = 30.0, clock=time.monotonic):
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self.clock = clock
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if self.clock() - self.opened_at >= self.cooldown_s:
            return False  # полуоткрытое состояние: пробуем снова
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.opened_at = self.clock()


_pool = ThreadPoolExecutor(max_workers=8)


def resilient_call(fn: Callable[[], T], breaker: CircuitBreaker, timeout_s: float = 2.0, retries: int = 1) -> T:
    if breaker.is_open:
        raise SourceUnavailable(f"{breaker.name}: circuit breaker открыт")
    last_error: Exception | None = None
    for _ in range(retries + 1):
        try:
            result = _pool.submit(fn).result(timeout=timeout_s)
            breaker.record_success()
            return result
        except FutureTimeout:
            last_error = SourceUnavailable(f"{breaker.name}: таймаут {timeout_s} с")
        except SourceUnavailable as exc:
            last_error = exc
    breaker.record_failure()
    raise last_error  # type: ignore[misc]


class FileBureau:
    """Имитация бюро: данные из JSON, поля latency_s и fail имитируют проблемы."""

    def __init__(self, path: Path):
        self.data = json.loads(path.read_text(encoding="utf-8"))

    def get_report(self, tax_id: str) -> BureauReport:
        rec = dict(self.data.get(tax_id, {}))
        time.sleep(rec.pop("latency_s", 0))
        if rec.pop("fail", False):
            raise SourceUnavailable("бюро: ошибка сервиса")
        return BureauReport(**rec)


class FileCoreBanking:
    def __init__(self, path: Path):
        self.data = json.loads(path.read_text(encoding="utf-8"))

    def get_profile(self, tax_id: str) -> CoreBankingProfile | None:
        rec = self.data.get(tax_id)
        return CoreBankingProfile(**rec) if rec else None  # None: клиент банку не известен
