"""Аудит-журнал решений (JSONL).

Каждая запись позволяет воспроизвести решение: хэш входных данных, версия
правил, версия модели, признаки, сработавшие правила и причины. ИНН хранится
в виде хэша: для расследования достаточно сопоставления, а журнал не
становится реестром клиентов.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class AuditLog:
    def __init__(self, path: Path | None):
        self.path = path
        self.records: list[dict] = []
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict) -> None:
        record = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), **record}
        self.records.append(record)
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
