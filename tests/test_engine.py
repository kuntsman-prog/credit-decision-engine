"""Тесты движка решений: правила, отказоустойчивость, объяснимость, аудит, теневой режим."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cde.audit import AuditLog
from cde.engine import ROOT, DecisionEngine, load_policy
from cde.integrations import CircuitBreaker, FileCoreBanking, SourceUnavailable, resilient_call
from cde.models import Application, BureauReport, Outcome
from cde.monitoring import psi
from cde.rules import RuleError, load_rules
from cde.scoring import ScoringModel

FIX = ROOT / "data" / "fixtures"


def app_(aid: str, **over) -> Application:
    data = json.loads((FIX / "applications" / f"{aid}.json").read_text(encoding="utf-8"))
    data.update(over)
    return Application(**data)


class StubBureau:
    """Бюро с заданным ответом или сбоем; считает вызовы."""

    def __init__(self, reports: dict | None = None, fail: bool = False):
        self.reports = reports or {}
        self.fail = fail
        self.calls = 0

    def get_report(self, tax_id: str) -> BureauReport:
        self.calls += 1
        if self.fail:
            raise SourceUnavailable("бюро: ошибка сервиса")
        return BureauReport(**self.reports.get(tax_id, {}))


def engine(bureau=None, challenger=True) -> DecisionEngine:
    fixtures = json.loads((FIX / "bureau.json").read_text(encoding="utf-8"))
    clean = {k: {kk: vv for kk, vv in v.items() if kk not in ("latency_s", "fail")} for k, v in fixtures.items()}
    return DecisionEngine(
        bureau or StubBureau(clean), FileCoreBanking(FIX / "core_banking.json"),
        rules=load_rules(ROOT / "config" / "rules.yaml"), policy=load_policy(ROOT / "config" / "policy.yaml"),
        champion=ScoringModel.load(ROOT / "models" / "champion.json"),
        challenger=ScoringModel.load(ROOT / "models" / "challenger.json") if challenger else None,
        audit=AuditLog(None), timeout_s=0.5)


# --- решения по демо-заявкам ---

def test_strong_company_approved_in_full():
    d = engine().decide(app_("A-001"))
    assert d.outcome == Outcome.APPROVE and d.approved_amount == d.requested_amount
    assert d.model.grade == "A"


def test_limit_reduced_to_keep_dscr():
    d = engine().decide(app_("A-002"))
    assert d.outcome == Outcome.APPROVE and 0 < d.approved_amount < d.requested_amount
    assert any("DSCR" in r for r in d.reasons)


def test_stop_factor_declines_regardless_of_score():
    d = engine().decide(app_("A-003"))
    assert d.outcome == Outcome.DECLINE
    assert {"SF-01", "SF-02"} <= {h.rule_id for h in d.rule_hits}


def test_refer_rule_overrides_good_score():
    d = engine().decide(app_("A-004"))
    assert d.model.grade in {"A", "B", "C"} and d.outcome == Outcome.REFER
    assert "RF-02" in {h.rule_id for h in d.rule_hits}


def test_new_client_with_large_amount_referred():
    d = engine().decide(app_("A-006"))
    assert d.outcome == Outcome.REFER and "RF-04" in {h.rule_id for h in d.rule_hits}


def test_high_pd_declined_with_reasons():
    d = engine().decide(app_("A-007"))
    assert d.outcome == Outcome.DECLINE and d.model.grade == "E"
    assert d.model.top_risk_factors and all(f.contribution > 0 for f in d.model.top_risk_factors)


# --- отказоустойчивость ---

def test_bureau_failure_never_auto_approves():
    d = engine(bureau=StubBureau(fail=True)).decide(app_("A-001"))
    assert d.outcome == Outcome.REFER and d.data_issues and d.approved_amount == 0


def test_stop_factor_still_applies_when_bureau_is_down():
    d = engine(bureau=StubBureau(fail=True)).decide(app_("A-001", years_in_business=0.5))
    assert d.outcome == Outcome.DECLINE and "SF-03" in {h.rule_id for h in d.rule_hits}


def test_circuit_breaker_stops_calling_failed_source():
    clock = [0.0]
    breaker = CircuitBreaker("бюро", failure_threshold=2, cooldown_s=30, clock=lambda: clock[0])
    bureau = StubBureau(fail=True)
    for _ in range(2):
        with pytest.raises(SourceUnavailable):
            resilient_call(lambda: bureau.get_report("1"), breaker, retries=0)
    calls = bureau.calls
    with pytest.raises(SourceUnavailable, match="circuit breaker"):
        resilient_call(lambda: bureau.get_report("1"), breaker, retries=0)
    assert bureau.calls == calls          # открытый выключатель не вызывает сервис
    clock[0] = 31.0
    bureau.fail = False
    resilient_call(lambda: bureau.get_report("1"), breaker, retries=0)
    assert not breaker.is_open            # после паузы сервис снова доступен


def test_idempotent_resubmission():
    e = engine()
    first = e.decide(app_("A-001"))
    second = e.decide(app_("A-001", requested_amount=5_000_000))
    assert second == first


# --- правила ---

def test_rules_reject_unsafe_definitions(tmp_path: Path):
    bad = tmp_path / "rules.yaml"
    bad.write_text('version: "x"\nrules:\n  - {id: X, kind: stop_factor, reason: r, when: [{field: os.system, op: eq, value: 1}]}\n', encoding="utf-8")
    with pytest.raises(RuleError):
        load_rules(bad)
    bad.write_text('version: "x"\nrules:\n  - {id: X, kind: stop_factor, reason: r, when: [{field: features.dscr, op: exec, value: 1}]}\n', encoding="utf-8")
    with pytest.raises(RuleError):
        load_rules(bad)


# --- объяснимость, аудит, теневой режим ---

def test_explanation_matches_model_math():
    e = engine()
    d = e.decide(app_("A-007"))
    m = e.champion
    z = {f: (d.features[f] - mu) / sd for f, mu, sd in zip(m.features, m.means, m.stds)}
    for factor in d.model.top_risk_factors:
        c = m.coef[m.features.index(factor.feature)]
        assert factor.contribution == pytest.approx(c * z[factor.feature], abs=1e-3)


def test_audit_record_allows_reproduction_without_raw_tax_id():
    e = engine()
    e.decide(app_("A-002"))
    rec = next(r for r in e.audit.records if r["event"] == "credit.decision.made")
    assert rec["ruleset_version"] and rec["model_version"] == "pd-corp-1.0" and rec["input_hash"]
    assert "301234562" not in json.dumps(rec, ensure_ascii=False)


def test_challenger_runs_in_shadow_and_does_not_change_decision():
    with_ch = engine(challenger=True).decide(app_("A-002"))
    without = engine(challenger=False).decide(app_("A-002"))
    assert with_ch.outcome == without.outcome and with_ch.approved_amount == without.approved_amount


def test_failing_shadow_does_not_break_decision():
    e = engine()
    e.challenger.features = e.challenger.features + ["unknown_feature"]
    d = e.decide(app_("A-001"))
    assert d.outcome == Outcome.APPROVE
    assert any(r["event"] == "shadow.skipped" for r in e.audit.records)


# --- мониторинг и API ---

def test_psi_detects_shift():
    expected = [0.1] * 10
    edges = [float(i) for i in range(1, 10)]
    assert psi(expected, edges, [i + 0.5 for i in range(10)] * 50) < 0.01
    assert psi(expected, edges, [0.5] * 500) > 0.25


def test_api_roundtrip():
    from cde import app as api
    client = TestClient(api.app)
    body = json.loads((FIX / "applications" / "A-001.json").read_text(encoding="utf-8"))
    body["application_id"] = "API-1"
    r = client.post("/v1/applications", json=body)
    assert r.status_code == 200 and r.json()["outcome"] == "APPROVE"
    assert client.get("/v1/decisions/API-1").json()["application_id"] == "API-1"
    assert client.get("/v1/decisions/none").status_code == 404
    assert client.get("/health").json()["champion"] == "pd-corp-1.0"
