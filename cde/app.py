"""HTTP API движка решений.

  uvicorn cde.app:app --port 8000
  POST /v1/applications        подать заявку, получить решение (идемпотентно по application_id)
  GET  /v1/decisions/{id}      получить ранее принятое решение
  GET  /health                 версии правил и моделей, состояние circuit breaker
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .cli import make_engine
from .engine import ROOT
from .models import Application, Decision

engine = make_engine(ROOT / "logs" / "decisions.jsonl")
app = FastAPI(title="Credit Decision Engine", version="1.0")


@app.post("/v1/applications", response_model=Decision)
def submit(application: Application) -> Decision:
    return engine.decide(application)


@app.get("/v1/decisions/{application_id}", response_model=Decision)
def get_decision(application_id: str) -> Decision:
    if application_id not in engine.decisions:
        raise HTTPException(status_code=404, detail="решение не найдено")
    return engine.decisions[application_id]


@app.get("/health")
def health() -> dict:
    return {
        "ruleset_version": engine.rules.version,
        "champion": engine.champion.version,
        "challenger": engine.challenger.version if engine.challenger else None,
        "bureau_breaker_open": engine.bureau_breaker.is_open,
        "core_banking_breaker_open": engine.cbs_breaker.is_open,
    }
