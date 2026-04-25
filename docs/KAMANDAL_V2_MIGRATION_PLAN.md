# Kamandal V2 Migration Plan

Date: 2026-04-25
Status: Working plan

## 1. Thesis

Kamandal V2 should be an incremental migration, not a rewrite.

The existing execution core is valuable because it is deterministic, auditable,
broker-aware, and already shaped around Google Sheets as a control tower. The
major missing pieces are:

- a real shadow portfolio engine that behaves like a paper account
- durable intelligence artifacts that let the system improve over time
- clear promotion rules between agent reasoning and execution-grade inputs
- a reflection loop that connects sources, strategies, orders, and outcomes

The execution core should remain conservative. The intelligence layer may become
agentic, but it must speak to the trading core through structured artifacts.

## 2. Target Shape

Kamandal V2 has four cooperating surfaces.

```text
Intelligence Layer
  -> source_observation
  -> idea_candidate
  -> playbook_insight
  -> source_intelligence
  -> policy_proposal
  -> promoted execution candidates

Execution Core
  -> portfolio sync
  -> market data and regime resolution
  -> strategy template resolution
  -> optimizer
  -> broker preflight
  -> order construction
  -> position management

Shadow Portfolio Engine
  -> broker-preflighted shadow fills
  -> shadow positions
  -> shadow portfolio snapshots
  -> mark-to-market and P&L
  -> simulated close/roll/adjust lifecycle

Google Sheet Cockpit
  -> observe
  -> approve
  -> override
  -> tune
  -> audit
```

## 3. Non-Negotiable Boundaries

1. The intelligence layer may propose candidates, strategy opinions, and policy
   changes. It must not silently mutate live execution policy.
2. The execution core consumes structured records only. It should not consume
   freeform agent prose directly.
3. Google Sheets remains the control tower for operator observation and
   approvals during the migration.
4. Shadow mode should become a real simulation account, not merely a trail of
   preflighted pending orders.
5. Live trading is out of scope until shadow mode has produced enough evidence
   to trust the loop.

## 4. Deployment Assumption

The first real deployment target is `oldmac`, running scheduled shadow loops.

The system should be able to:

- run unattended during market hours
- preflight candidate orders with the broker
- assume successful shadow fills when preflight passes
- maintain a simulated portfolio over days or weeks
- expose daily state through Google Sheets
- leave enough audit trail to diagnose bad behavior after the fact

## 5. Phase 1: Real Shadow Portfolio

### Goal

Make shadow mode behave like a paper account with a starting bankroll.

Today shadow mode writes/preflights `PendingOrder` records. V2 shadow mode should
also create simulated fills and maintain a portfolio that future optimizer and
position-manager runs use as state.

### Core Decisions

- A broker preflight success is the trigger for a shadow fill candidate.
- Shadow fills are explicit artifacts, not inferred later from `PendingOrder`.
- Shadow positions are separate from live broker positions.
- In shadow mode, optimizer state should come from the shadow portfolio, not the
  live Public portfolio.
- The shadow account should support a configured starting NLV.

### Likely New Artifacts

- `ShadowFill`
  - fill id
  - pending order id
  - plan id
  - action
  - underlying
  - strategy id
  - legs
  - quantity
  - fill price
  - filled at
  - estimated BPR
  - greeks impact
  - preflight response pointer

- `ShadowPosition`
  - position id
  - source fill id
  - underlying
  - strategy id
  - strategy type
  - legs
  - open date
  - open credit/debit
  - current value
  - BPR
  - greeks
  - status
  - management status

- `ShadowPortfolioSnapshot`
  - timestamp
  - account id or run id
  - starting NLV
  - current NLV
  - cash balance or cash proxy
  - BPR used
  - Greeks
  - position count
  - open positions

### Acceptance Criteria

- Running `python -m vol_crush.main --skip-backtest` in shadow mode can produce
  preflighted orders and convert successful preflights into shadow fills.
- A later optimizer run sees the shadow positions and does not repeatedly open
  the same exposure as if the account were empty.
- Position manager can evaluate shadow positions without requiring live broker
  positions.
- Google Sheets shows enough daily state to inspect the shadow account.
- The audit trail can answer: what was proposed, what was preflighted, what was
  assumed filled, and what portfolio state resulted.

### Deferred From Phase 1

- perfect fill simulation
- commission/slippage model
- intraday option mark quality beyond available fixtures/provider data
- live order submission
- fully autonomous agent-generated candidates

## 6. Phase 2: Intelligence Artifacts

### Goal

Give the intelligence layer durable, queryable state.

This phase is not about fancy memory. It is about creating the right facts so
future memory and agents have something solid to build on.

### New Artifacts

- `source_observation`
  - one row per ingested source item
  - records source type, title, author, URL, published time, lane assignment,
    actionable value, playbook value, digest value, confidence, and evidence

- `idea_candidate`
  - intermediate object before `idea_review`
  - records whether the idea is promotable, rejected, duplicate, stale,
    educational-only, or execution-grade

- `playbook_insight`
  - durable general trading lesson
  - applies to regimes, strategy types, underlyings, or source families
  - does not directly change execution policy

- `source_intelligence`
  - aggregated scorecard by source/channel/family
  - tracks actionable rate, false-positive rate, playbook rate, digest value,
    and downstream conversion to shadow plans/orders/outcomes

- `policy_proposal`
  - proposed change to strategy templates, source priority, filters, prompts, or
    execution policy
  - requires operator approval for material trading-policy changes

### Acceptance Criteria

- Every ingested document receives a `source_observation`.
- `idea_review` becomes quieter and contains only promoted candidates.
- Useful non-trade content can become digest or playbook memory instead of being
  forced into trade ideas.
- Source quality can be reviewed from stored data rather than vibes.

## 7. Phase 3: Agent-Generated Candidates

### Goal

Allow the intelligence layer to create candidates from recent context,
playbook memory, and current opportunity categories.

This is the long-term flex: the system should eventually answer questions like:

- Is a calendar, strangle, spread, or no-trade better in this regime?
- Which strategy family is most appropriate given what it has heard recently?
- Are sources converging on a useful opportunity, or just creating noise?
- Which symbols deserve attention even if no source mentioned them today?

### Guardrails

- Agent candidates must be structured records.
- Shadow mode may accept broader candidate generation.
- Live mode requires stricter approval gates.
- Strategy/risk policy changes remain proposal-based.
- First implementation slice is deterministic and opt-in:
  `intelligence.agent_candidates.enabled: true`.
- Generated candidates are labeled with `agent_` idea ids, show/source
  `agent_generated`, and `kamandal://agent/opportunity` as the source URL.

### Acceptance Criteria

- The intelligence layer can create an `idea_candidate` without a direct source
  ticker mention, based on a named opportunity class.
- The optimizer can treat agent-generated candidates and source-derived
  candidates through the same structured interface.
- The cockpit clearly distinguishes source-derived candidates from
  agent-generated candidates.

### First Slice Implemented

- Regime + configured strategy templates + market snapshots can generate a small
  set of shadow-only `TradeIdea` records.
- The optimizer validates those records through the same strategy/fixture path
  as source-derived ideas.
- Live mode ignores the generator entirely.
- Google Sheets `daily_plan` notes include `source=agent_generated` for any
  selected generated candidate.

## 8. Phase 4: Reflection Loop

### Goal

Make the system improve from outcomes.

Reflection connects:

- what was heard
- what was classified
- what was promoted
- what the optimizer selected
- what shadow preflighted
- what shadow assumed filled
- what happened afterward

### Outputs

- daily reflection summary
- source intelligence updates
- idea quality assessments
- playbook insight updates
- policy proposals
- ops/anomaly report when the loop misbehaves

### Acceptance Criteria

- The system can identify noisy sources and high-value sources.
- The system can identify candidate types that repeatedly fail optimizer or
  position-management checks.
- The system can propose prompt, filter, source, or strategy-template changes
  with evidence.
- Operator can audit the reasoning from Google Sheets or local artifacts.

## 9. Suggested Repo Shape

Keep one repo and the existing package name for now.

Potential module additions:

```text
vol_crush/intelligence/
  artifacts.py
  intake.py
  reflection.py
  source_scoring.py

vol_crush/shadow/
  fills.py
  portfolio.py
  service.py
  __main__.py
```

Existing modules that should mostly stay stable:

- `vol_crush/optimizer`
- `vol_crush/executor`
- `vol_crush/position_manager`
- `vol_crush/position_grouping`
- `vol_crush/integrations/public_broker`
- `vol_crush/sheets`

Expected touchpoints:

- `executor` creates shadow fills after successful preflight
- `optimizer` chooses portfolio source based on execution mode
- `position_manager` learns to evaluate shadow positions in shadow mode
- `sheets` publishes shadow account state and intelligence summaries
- `storage` persists new artifacts

## 10. First Implementation Slice

Start with Phase 1.

### Slice 1A: Storage and Models

- Add shadow fill model.
- Add shadow portfolio/position model, or reuse `Position` with an explicit
  shadow source if that stays clean.
- Add SQLite tables and list/save methods.

### Slice 1B: Executor Integration

- After Public preflight succeeds in shadow mode, create a shadow fill.
- Mark the related order as shadow-filled or preflight-filled.
- Preserve broker preflight response for audit.

### Slice 1C: Shadow Portfolio Projection

- Convert shadow fills into open shadow positions.
- Aggregate shadow positions into a portfolio snapshot.
- Make optimizer use the shadow snapshot when execution mode is shadow.

### Slice 1D: Cockpit Visibility

- Push shadow positions and current shadow portfolio state to the existing
  positions/daily plan surfaces, or add a small dedicated tab later if needed.

### Slice 1E: Tests

- Executor test: successful shadow preflight creates a shadow fill.
- Optimizer test: second run sees shadow exposure.
- Position-manager test: shadow position can be evaluated.
- Storage test: shadow artifacts round-trip through SQLite.

## 11. Open Questions

These do not block Phase 1, but should be answered before Phase 3.

1. Should shadow fills assume immediate fill at optimizer target price,
   preflight mid/estimated price, or a configurable conservative price?
2. Should shadow mode simulate partial fills, or only all-or-none fills for now?
3. Should source intelligence be visible in Google Sheets immediately, or remain
   internal until signal quality improves?
4. What approval gate should exist before agent-generated candidates enter the
   optimizer in shadow mode?
5. What evidence threshold should be required before a playbook insight can
   become a policy proposal?

## 12. Current Landing Point

The V2 migration begins with shadow fidelity, not agent memory.

Reason:

- shadow fidelity gives a measurable laboratory
- measurable outcomes make later intelligence meaningful
- intelligence without outcome linkage becomes decoration
- live-trading confidence depends on weeks of believable shadow behavior

Once Phase 1 is reliable, Phases 2-4 can make the system smarter without
destabilizing the execution core.
