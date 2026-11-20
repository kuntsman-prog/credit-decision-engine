# Credit Decision Engine for corporate lending

**English** | [Русский](README.ru.md)

A learning but working prototype of an automated corporate lending platform. It shows how a credit decision is made: integrations with a credit bureau and the core banking system, declarative policy rules, ML scoring with an explanation for every decision, limit calculation, shadow mode for a new model version, audit and stability monitoring.

Demo data is fictitious and models are trained on a synthetic portfolio. Amounts are in US dollars. Rule texts and decision reasons are in Russian.

## What the project demonstrates

| Capability | Implementation |
|---|---|
| Bureau and core banking integration | ports and adapters, timeouts, retries, circuit breaker |
| Fail-safe behaviour | unavailable data or model leads to manual review, never to auto-approval |
| Policy rules | stop factors and referral rules in YAML; a restricted condition language with no eval, validated at load time |
| ML scoring | logistic regression, 12-month PD, scorecard points, rating grade |
| Explainability | each feature's contribution to the decision, top risk factors in analyst language |
| Limit | the amount is sized so that debt service coverage (DSCR) stays at or above 1.25 and the loan does not exceed 80% of collateral |
| Champion and challenger | the new model version scores every application in shadow mode without affecting the decision |
| Events | an event bus with topics mirroring Kafka; a failing subscriber does not break the decision |
| Audit | input hash, ruleset and model versions, features, reasons; tax ID stored only as a hash |
| Monitoring | PSI for model features |
| Idempotency | resubmitting an application with the same ID returns the original decision |

Target microservice architecture, C4, events and model risk management: [ARCHITECTURE.md](ARCHITECTURE.md) (in Russian).

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m cde.cli decide            # decisions for all demo applications
python -m cde.cli decide A-002      # details: features, rules, reasons, factor contributions
python -m cde.cli shadow            # champion vs challenger comparison
python -m cde.cli monitor           # feature stability under a shifted application flow
python -m cde.cli train             # retrain models on synthetic data
python -m pytest                    # tests

uvicorn cde.app:app --port 8000     # HTTP API: POST /v1/applications, GET /v1/decisions/{id}, GET /health
```

## Decisions on demo applications

| Application | Outcome | Amount | PD, grade | Why |
|---|---|---|---|---|
| A-001 | APPROVE | 1,000,000 of 1,000,000 | 0.2%, A | strong financials, clean credit history |
| A-002 | APPROVE | 1,300,000 of 1,400,000 | 4.5%, C | limit reduced to keep DSCR at or above 1.25 |
| A-003 | DECLINE | 0 | 18.7%, E | stop factors: active default, 90+ days past due |
| A-004 | REFER | 0 | 2.9%, C | good score, but the loan exceeds 80% of collateral |
| A-005 | REFER | 0 | not scored | the bureau did not respond in time |
| A-006 | REFER | 0 | 0.5%, A | new client with a large amount |
| A-007 | DECLINE | 0 | 45.0%, E | high probability of default; risk factors shown |

## Models

| Model | Features | AUC | Gini | KS | Approval rate | Bad rate among approved |
|---|---|---|---|---|---|---|
| champion pd-corp-1.0 | DSCR, debt to EBITDA, LTV, years in business, 30+ delinquencies, revenue | 0.824 | 0.648 | 0.506 | 55.6% | 2.25% |
| challenger pd-corp-1.1 | the same plus bureau inquiries in 6 months | 0.830 | 0.660 | 0.522 | 58.1% | 2.24% |

Metrics are computed on a 10,000-application holdout; approval and bad rates use the grade policy without rules.

Shadow run on a flow of 5,000 applications with known outcomes (`python -m cde.cli shadow`):
- grade-based decisions agree in 87.9% of cases;
- 230 applications would be approved only by the challenger, with an actual default rate of 5.7%;
- 114 applications would be approved only by the champion, with an actual default rate of 10.5%;
- bad rate among approved: champion 2.29%, challenger 2.23%.

The new version approves more applications without a higher bad rate among approved. This is evidence for a model committee, not an automatic reason to switch.

## Stability monitoring

`python -m cde.cli monitor` simulates rising rates: debt service coverage of new applicants drops by 25%. PSI for DSCR is 0.54 (a significant shift, above 0.25), for the other features below 0.01. Such a signal means the model must be reviewed before the actual default rate changes.

## Regulation

Scoring of legal entities is not on the EU AI Act list of high-risk AI systems: Annex III covers creditworthiness assessment of natural persons. If the engine were applied to sole proprietors or individuals, high-risk requirements would apply. For a bank, any scoring model remains subject to model risk management: independent validation, monitoring, documentation. More in [ARCHITECTURE.md](ARCHITECTURE.md).

## Project structure

```
cde/
  models.py        application, bureau and core banking data, decision
  integrations.py  ports and adapters, timeouts, retries, circuit breaker
  features.py      financial ratios, annuity, inverse limit calculation
  rules.py         rule language, loading and validation, evaluation
  scoring.py       PD, scorecard points, grade, feature contributions
  training.py      synthetic portfolio, training, metrics, backtest
  engine.py        decision flow, policy, limit, audit, shadow mode
  events.py        event bus with Kafka topics
  audit.py         decision log
  monitoring.py    PSI
  cli.py, app.py   command line and HTTP API
config/rules.yaml    credit policy rules
config/policy.yaml   grades, thresholds, limit parameters
models/              champion and challenger with metrics and distributions
data/fixtures/       demo applications, bureau and core banking responses
tests/               automated tests
```

## Limitations

Models are trained on synthetic data with a predefined dependency of default on features; real decisions require the bank's historical data, independent validation and model approval. Policy thresholds and the interest rate are illustrative.
