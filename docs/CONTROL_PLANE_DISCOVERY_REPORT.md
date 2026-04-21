# Kamandal Control Plane Discovery Report

**Date:** April 21, 2026
**Status:** Discovery / Planning
**Author:** Codex

---

## 1. Executive Summary

Kamandal does not currently have a "mysterious AI black box" problem in the core
trading engine. The core state is already local-first:

- SQLite is the main operational store.
- JSON audit mirrors provide readable history.
- Grouped position logic and portfolio risk logic are explicit and test-backed.

The current friction comes from the control plane. Runtime behavior is split
across:

- local config files
- local SQLite state
- Google Sheet tabs
- `data/sheet_cache/*.json` mirrors populated from those tabs

That split creates an operator experience that feels heavier than it needs to.
The repo has one trading system and one spreadsheet-driven control surface layered
on top of it. The control surface is where most of the accidental complexity lives.

### Bottom line

The recommended direction is not "remove Google Sheets."

The recommended direction is:

- keep Google Sheets for visibility and deliberate overrides
- stop using Google Sheets as broad internal plumbing
- reduce the number of tabs that materially affect runtime behavior
- keep safety-critical engine logic local and deterministic

---

## 2. Questions This Report Answers

This report is meant to answer five questions before any refactor:

1. What does a trader/operator have to manage today?
2. Which Google Sheet tabs only provide visibility?
3. Which tabs materially change runtime behavior?
4. Which parts of the repo are complex for good reasons?
5. What refactor path preserves transparency without increasing automation risk?

---

## 3. Current System Ownership

### 3.1 Real source of truth

The local store in `vol_crush/integrations/storage.py` is already the primary
operational system of record.

Stored locally:

- `trade_ideas`
- `raw_documents`
- `positions`
- `broker_position_legs`
- `portfolio_snapshots`
- `trade_plans`
- `pending_orders`
- `backtest_results`
- `replay_trades`
- `fixtures`

This means Kamandal is already structurally capable of running without Sheets as
its underlying database.

### 3.2 What Sheets currently are

Sheets are not just a dashboard.

They currently act as a mix of:

- visibility layer
- operator override layer
- approval gate layer
- config overlay layer

That mixed role is the main source of cognitive load.

---

## 4. Current Daily Workflow

The daily orchestration flow in `vol_crush/main.py` is:

1. Optionally fetch source content and extract ideas.
2. Pull Google Sheet tabs into local `data/sheet_cache/*.json`.
3. Push recent extracted ideas into `idea_review`.
4. Refresh fixtures and replay trades.
5. Optionally run backtests.
6. Sync broker portfolio.
7. Build a trade plan.
8. Run the executor.
9. Run the position manager.
10. Push the latest `daily_plan` rows to Sheets.

### 4.1 Operator touchpoints in practice

From a trader/operator perspective, the likely touchpoints today are:

- enable or disable strategies
- modify structure-level defaults
- modify profile-level caps
- modify universe membership
- optionally override the daily regime
- review extracted ideas
- approve or reject ideas
- review the proposed daily plan
- approve the daily plan
- inspect positions and portfolio state

### 4.2 Hidden operational cost

There is a two-run approval pattern built into the current system:

- the plan is generated in one run
- the operator approves it in Sheets
- the executor checks approval from cached `daily_plan` rows on a later run

That makes the system feel more procedural than conversational.

---

## 5. Google Sheets Dependency Audit

### 5.1 Tab inventory

| Tab | Current role | Affects runtime behavior? | Notes |
|---|---|---:|---|
| `strategies` | strategy enablement and live/shadow gate | Yes | Used by optimizer as approval overlay |
| `template_library` | structure-level parameter override | Yes | Changes template defaults at runtime |
| `regime_control` | day-level regime override | Yes | Can override evaluator for today |
| `profiles` | profile-level risk override | Yes | Changes profile caps at runtime |
| `universe` | symbol membership override | Yes | Changes eligible symbols at runtime |
| `idea_review` | idea display, approval gate, manual idea entry | Yes | Blocks or admits ideas |
| `daily_plan` | plan display and approval gate | Yes | Blocks executor unless approved or bypassed |
| `positions` | grouped position report | Not currently in main flow | Writer exists but does not appear to be wired into orchestration |

### 5.2 Classification by function

#### Visibility

- `idea_review`
- `daily_plan`
- `positions`

#### Override

- `template_library`
- `profiles`
- `universe`
- `regime_control`

#### Approval gate

- `strategies`
- `idea_review`
- `daily_plan`

#### Internal plumbing

- `data/sheet_cache/*.json`
- `idea_review_metadata.json`

### 5.3 Important observation

Several tabs are more powerful than they look.

The operator might reasonably think:

- `strategies` is just a simple list of approved rows
- `template_library` is just documentation
- `profiles` and `universe` are just references

But in practice these are active runtime overlays consumed by the optimizer.

---

## 6. Tab-by-Tab Findings

### 6.1 `strategies`

**Current behavior**

- controls whether a resolved strategy is considered eligible
- in live mode, also controls whether the row is allowed to graduate from
  `shadow` to `live`
- can override allowed regimes, IV bounds, earnings behavior, and allocation caps

**Assessment**

- valuable as an operator-facing control
- too powerful for a tab that looks simple
- overlaps with template and profile controls

**Recommendation**

- keep
- narrow its purpose to "what is enabled and what is live-eligible"
- move deeper parameter editing elsewhere or make it clearly advanced

### 6.2 `template_library`

**Current behavior**

- overrides structure-level strategy defaults at runtime
- duplicates concepts already defined in `config/strategy_templates.yaml`

**Assessment**

- useful for experimentation
- high-complexity surface area
- likely not something a trader wants to tune daily

**Recommendation**

- downgrade from everyday control surface
- either merge into a single advanced control tab or move back to config-only

### 6.3 `profiles`

**Current behavior**

- overrides stock-profile-level caps and rules

**Assessment**

- legitimate knob
- probably too granular as a dedicated daily-use tab

**Recommendation**

- merge into a consolidated control surface

### 6.4 `universe`

**Current behavior**

- changes which symbols belong to which profile
- changes runtime eligibility

**Assessment**

- useful and trader-relevant
- should stay visible
- can likely be simplified

**Recommendation**

- keep in some form
- consider merging with profile control under a smaller "universe control" model

### 6.5 `regime_control`

**Current behavior**

- overrides the evaluator for today's regime if enabled

**Assessment**

- high-value override
- easy to explain
- low cognitive load

**Recommendation**

- keep
- this is one of the cleanest examples of a good Sheet control

### 6.6 `idea_review`

**Current behavior**

- displays extracted ideas
- acts as idea approval gate
- can also create operator-entered ideas

**Assessment**

- worth keeping
- closest thing to a natural human review queue
- but the visible sheet columns are too thin relative to the actual metadata

**Important issue**

The visible row schema is minimal, while richer metadata is preserved separately
in `idea_review_metadata.json`. That means the visible sheet is not the complete
truth from the operator's point of view.

**Recommendation**

- keep
- make the visible columns more complete
- reduce hidden sidecar metadata

### 6.7 `daily_plan`

**Current behavior**

- receives the latest plan rows
- acts as executor approval gate

**Assessment**

- worth keeping as a visible approval checkpoint
- current schema is too compact
- forces a multi-run workflow

**Recommendation**

- keep as an approval surface
- make it richer and more explicit
- eventually pair it with a clearer state machine

### 6.8 `positions`

**Current behavior**

- writer exists for grouped position reporting
- not clearly wired into the main orchestration path today

**Assessment**

- highly valuable if promoted
- currently underused

**Recommendation**

- promote it to a first-class visibility tab

---

## 7. Safety-Critical Complexity vs Accidental Complexity

### 7.1 Complexity worth preserving

The following complexity appears justified and should largely remain:

- grouped position classification
- reconciliation of broker legs to known Kamandal orders
- refusal to auto-manage unknown or orphaned structures
- full-group close assertions
- portfolio-level Greek and BPR constraints
- separation of raw broker legs from grouped positions

This complexity protects against incorrect automation.

### 7.2 Complexity likely to simplify

The following complexity appears mostly control-plane overhead:

- many sheet tabs with overlapping authority
- sheet cache indirection before runtime decisions
- hidden metadata sidecars for operator-facing review tabs
- approval choreography spread across multiple tabs and runs
- multiple places to tune strategy behavior
- docs and runtime no longer telling a single consistent story

---

## 8. Current Pain Points for a Trader/Operator

The repo currently asks the operator to think at too many levels at once:

- strategy template level
- profile level
- symbol universe level
- idea approval level
- plan approval level
- regime override level

The granularity is often sensible, but too much of it is exposed at the same
time. The result is a cockpit with many dials, but not a clear distinction
between:

- knobs you use every day
- knobs you touch rarely
- read-only diagnostics

That is the core usability problem.

---

## 9. Target Control Plane Principles

The target state should preserve transparency while reducing operator overhead.

### Principle 1: Local state owns reality

SQLite and audit JSON remain the operational truth.

### Principle 2: Sheets stay visible and editable

Sheets remain the trader-facing cockpit so the system does not become opaque.

### Principle 3: Only a few sheet edits should change behavior

If everything can change runtime behavior, the control plane becomes hard to
trust.

### Principle 4: Advanced tuning should be separated from daily operations

Daily workflow should be small and explicit.

### Principle 5: The agent should operate on top of the cockpit

The agent should read local state, sync the cockpit, explain recommendations,
and respect your overrides. The agent should not depend on hidden spreadsheet
plumbing.

---

## 10. Proposed Target Operator Surface

The likely end state is a smaller-sheet model with no more than five tabs:

| Proposed tab | Purpose |
|---|---|
| `control` | execution mode, regime override, high-level strategy toggles, pause switches |
| `ideas` | review queue for extracted ideas and operator-entered ideas |
| `plan` | today's proposed actions and approvals |
| `positions` | grouped positions with health and management state |
| `risk` | portfolio snapshot, limits, utilization, and notable blockers |

### What likely moves out of day-to-day Sheets

- structure-level template editing
- profile-level deep configuration
- multi-tab overlays for the same concept
- hidden metadata stored outside visible operator views

These can still exist, but they should be treated as advanced configuration, not
daily operator workflow.

---

## 11. Decision Matrix

| Current surface | Recommendation | Why |
|---|---|---|
| `strategies` | Keep, narrow | High-value operator control, but currently too broad |
| `template_library` | Merge or downgrade | Advanced tuning, not ideal daily cockpit surface |
| `profiles` | Merge | Useful but too specialized as a standalone tab |
| `universe` | Keep in simplified form | Trader-relevant and operationally important |
| `regime_control` | Keep | Clean, understandable override |
| `idea_review` | Keep, enrich | Natural human review queue |
| `daily_plan` | Keep, enrich | Important approval point, currently too thin |
| `positions` | Promote | High-value visibility surface |
| sheet cache JSON | Reduce criticality over time | Good as implementation detail, bad as operator concept |

---

## 12. Recommended Phased Migration

### Phase 0: Lock the current contract in writing

Before any refactor:

- declare what each current tab owns
- declare which ones are authoritative
- declare which runtime paths consume them

This report is the first draft of that contract.

### Phase 1: Improve visibility without changing behavior

Goals:

- make `idea_review` and `daily_plan` columns richer
- wire `positions` into the daily flow if desired
- expose enough information that the operator can understand decisions without
  reading logs or cache files

This is the safest first change.

### Phase 2: Separate daily controls from advanced controls

Goals:

- identify which knobs are genuinely daily-use
- move advanced template/profile tuning out of the everyday cockpit
- reduce the number of tabs that can silently change optimizer behavior

### Phase 3: Simplify approval choreography

Goals:

- keep idea and plan approval
- reduce multi-run friction where possible
- make approval state explicit rather than inferred from sparse rows

### Phase 4: Agent-first operations on top of the simplified cockpit

Goals:

- agent summarizes risk and plan
- agent syncs the cockpit
- operator uses Sheets as oversight and override surface
- agent only escalates on low confidence, policy conflict, or approval gates

---

## 13. Risks If Nothing Changes

If the current model remains as-is:

- the operator burden will likely keep growing as more automation is added
- the system will feel harder to trust because authority is split
- onboarding new behavior will keep increasing tab and config complexity
- the AI agent layer will inherit spreadsheet choreography instead of a clean
  operating model

---

## 14. Recommended Next Deliverable

The next artifact should be a concrete "Control Plane Proposal" that specifies:

- the target tab set
- the owner of each tab
- which fields are read-only vs editable
- which edits are allowed to change runtime behavior
- which current tabs will be merged, downgraded, or retired

That proposal should be written before code changes begin.

---

## 15. Appendix: Key Code Areas Reviewed

- `vol_crush/main.py`
- `vol_crush/integrations/storage.py`
- `vol_crush/optimizer/service.py`
- `vol_crush/executor/service.py`
- `vol_crush/portfolio_sync/service.py`
- `vol_crush/position_grouping/service.py`
- `vol_crush/position_manager/service.py`
- `vol_crush/sheets/sync.py`
- `vol_crush/sheets/schemas.py`
- `config/config.example.yaml`
- `README.md`
- `docs/MICRO_PHASE_PLAN.md`

