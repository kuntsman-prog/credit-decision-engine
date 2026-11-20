"""Шина событий. В демо работает в одном процессе, в промышленном варианте
каждому топику соответствует топик Kafka, а подписчики становятся
отдельными сервисами (см. ARCHITECTURE.md).

Сбой подписчика изолирован: теневая модель или аудит-сервис не должны
ломать выдачу решения клиенту. Ошибки подписчиков сохраняются для мониторинга.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

APPLICATION_RECEIVED = "credit.application.received"
APPLICATION_ENRICHED = "credit.application.enriched"
DECISION_MADE = "credit.decision.made"


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[Callable[[dict], Any]]] = defaultdict(list)
        self.errors: list[dict] = []
        self.published: list[tuple[str, str]] = []

    def subscribe(self, topic: str, handler: Callable[[dict], Any]) -> None:
        self._subs[topic].append(handler)

    def publish(self, topic: str, event: dict) -> None:
        self.published.append((topic, event.get("application_id", "")))
        for handler in self._subs[topic]:
            try:
                handler(event)
            except Exception as exc:  # noqa: BLE001
                self.errors.append({"topic": topic, "handler": getattr(handler, "__name__", str(handler)), "error": str(exc)})
