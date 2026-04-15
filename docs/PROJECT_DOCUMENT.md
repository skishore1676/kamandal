# Kamandal - Project Document v0.2

**Date:** March 31, 2026
**Status:** Discovery / Planning
**Author:** Product & Engineering

---

## 1. Executive Summary

**Kamandal** is an options portfolio management system that turns tastytrade-style
premium-selling wisdom into a disciplined, Greek-optimized execution machine.

The core loop:

```
Approved Strategies (config)
        |
Daily Idea Capture (live tastytrade content)
        |
Portfolio Optimizer (deterministic Greek engine + LLM refinement)
        |
Trade Execution (broker API)
        |
Position Management & Adjustment (the edge)
```

The system is NOT a generic trading bot. It is a **portfolio-level Greek optimizer**
that selects from a stream of daily trade ideas, choosing the combination that best
improves the portfolio's delta, gamma, and theta profile. That selection logic is
where the edge lives.

---

## 2. Problem Statement

| Bottleneck | Description |
|---|---|
| **Information Overload** | Hundreds of hours of tastytrade content; strategies scattered across shows and guests |
| **Strategy Discipline** | Discretionary judgment creeps in; hard to stay mechanical |
| **Trade-Level vs Portfolio-Level** | Most retail traders think position-by-position, not in terms of aggregate Greeks |
| **Idea-to-Execution Gap** | Hearing a great trade idea on a show and actually sizing/executing it optimally are different skills |

Kamandal closes all four gaps with an automated pipeline from content to execution.

---

## 3. Competitive Landscape

### 3.1 Existing Tools

| Name | What It Does | Gap vs. Kamandal |
|---|---|---|
| **tastytrade platform** | Broker + education; portfolio Greek views | No AI idea capture, no autonomous optimization loop |
| **OptionAlpha** | Visual bot builder for options automation | Rule-based single-strategy bots; no portfolio-level optimization |
| **ORATS** | Backtesting, scanners, options data APIs | Data/analytics only; no content intelligence, no execution |
| **QuantConnect / QuantLib** | Open-source quant frameworks | General-purpose; no tastytrade-specific pipeline |
| **tastytrade-sdk / tastytrade-api** | Python SDK for tastytrade brokerage | API wrapper only; no intelligence layer |
| **r/thetagang scripts** | Community theta-selling scripts | Fragmented; no portfolio optimization |
| **public.com** | Broker with options support | Execution venue only; no research/optimization layer |

### 3.2 What Makes Kamandal Unique

Nobody has built the combination of:
- **Live content intelligence** (daily idea capture from video/audio)
- **Deterministic portfolio optimization** (Greek-aware idea selection)
- **LLM-augmented position management** (refinement loop for adjustments)
- **End-to-end automation** (content in, optimized trades out)

### 3.3 MCP Servers / Existing Integrations

There are community MCP servers for tastytrade. Decision: **build from the ground up**.
Rationale: we need tight control over order construction, Greek calculations, and the
optimization loop. We can revisit MCP integrations later if they add value, but the
core pipeline should be ours.

---

## 4. System Architecture

```
 ONE-TIME SETUP (Phase 0)                    DAILY OPERATING LOOP
 ========================                    =====================

 ┌─────────────────────┐
 │  AGENT 0:           │
 │  Strategy Miner     │
 │                     │
 │  data/transcripts/  │──── LLM extraction ──── Human Review ──── config/strategies.yaml
 │  (10+ historical    │                          & Approval        (locked strategies
 │   transcripts)      │                                             + parameter bounds)
 └─────────────────────┘

                              ┌─────────────────────────────────────────────────┐
                              │              DAILY PIPELINE                      │
                              │                                                 │
                              │  ┌───────────────┐                              │
                              │  │ AGENT 1:      │                              │
                              │  │ Live Idea     │  YouTube (now)               │
                              │  │ Scraper       │  X, other sources (later)    │
                              │  │               │                              │
                              │  │ Modes:        │                              │
                              │  │ - Record+transcribe                          │
                              │  │ - Live transcript                            │
                              │  └──────┬────────┘                              │
                              │         │                                       │
                              │         ▼                                       │
                              │  ┌─────────────┐    Today's Ideas               │
                              │  │ GSheet:     │    (e.g. 5 new ideas)          │
                              │  │ daily_ideas │                                │
                              │  └──────┬──────┘                                │
                              │         │                                       │
                              │         ▼                                       │
                              │  ┌──────────────────────────────┐               │
                              │  │ PORTFOLIO OPTIMIZER          │               │
                              │  │ (THE EDGE)                   │               │
                              │  │                              │               │
                              │  │ Current Portfolio: 3 pos     │               │
                              │  │ Candidate Ideas:   5 new     │               │
                              │  │                              │               │
                              │  │ ┌──────────────────────┐     │               │
                              │  │ │ Deterministic Engine │     │               │
                              │  │ │ - Enumerate combos   │     │               │
                              │  │ │ - Score delta/gamma/ │     │               │
                              │  │ │   theta improvement  │     │               │
                              │  │ │ - Check constraints  │     │               │
                              │  │ │ - Rank candidates    │     │               │
                              │  │ └──────────┬───────────┘     │               │
                              │  │            │                 │               │
                              │  │            ▼                 │               │
                              │  │ ┌──────────────────────┐     │               │
                              │  │ │ LLM Refinement Loop  │     │               │
                              │  │ │ - Interpret market    │     │               │
                              │  │ │   context             │     │               │
                              │  │ │ - Challenge optimizer │     │               │
                              │  │ │   output              │     │               │
                              │  │ │ - Suggest adjustments │     │               │
                              │  │ │   to existing pos     │     │               │
                              │  │ └──────────┬───────────┘     │               │
                              │  │            │                 │               │
                              │  │            ▼                 │               │
                              │  │   Approved Trade Plan        │               │
                              │  └──────────────┬───────────────┘               │
                              │                 │                               │
                              │                 ▼                               │
                              │  ┌──────────────────────┐                       │
                              │  │ EXECUTOR             │                       │
                              │  │ - Size positions     │                       │
                              │  │ - Route orders       │                       │
                              │  │ - Monitor fills      │                       │
                              │  │ - Log to GSheet      │                       │
                              │  │                      │                       │
                              │  │ Broker: tastytrade,  │                       │
                              │  │ Public, or Schwab    │                       │
                              │  │ (TBD)                │                       │
                              │  └──────────────────────┘                       │
                              │                                                 │
                              └─────────────────────────────────────────────────┘
```

---

## 5. Module Breakdown

### Module 0: Strategy Miner (One-Time Setup)

| Attribute | Detail |
|---|---|
| **Purpose** | Extract trading strategies from historical tastytrade transcripts |
| **Input** | `data/transcripts/*.txt` (10+ transcripts to start, more added over time) |
| **Processing** | LLM reads transcripts, extracts strategy definitions with parameters |
| **Output** | Candidate strategies presented for human review |
| **Human Gate** | User approves strategies and sets parameter boundaries |
| **Final Output** | `config/strategies.yaml` - locked strategy definitions used by the rest of the pipeline |
| **Frequency** | One-time at setup. Rarely re-run (only when new strategy types emerge) |

**Strategy Card (in config/strategies.yaml):**
```yaml
strategies:
  - id: core_strangle
    name: "Core Short Strangle"
    structure: short_strangle
    underlyings: [SPY, IWM, QQQ, TLT, GLD]
    filters:
      iv_rank_min: 30
      dte_range: [30, 45]
      delta_range: [0.14, 0.18]  # each side
    management:
      profit_target_pct: 50
      max_loss_multiple: 2.0  # close at 2x credit received
      roll_dte_trigger: 21
    allocation:
      max_bpr_pct: 30
    backtest_approved: true
    dry_run_passed: true
```

---

### Module 1: Live Idea Scraper (Daily)

| Attribute | Detail |
|---|---|
| **Purpose** | Capture specific trade ideas and executions from daily tastytrade content |
| **Input Sources** | YouTube channel uploads (primary), local transcript directories, RSS feeds, generic web pages; X posts and live-stream audio (later) |
| **Capture Modes** | (a) `idea_sources --source youtube` polls Atom feeds + pulls captions via `youtube-transcript-api`; (b) `idea_scraper --mode live`/`record` records audio and runs Whisper (OpenAI provider only) |
| **Processing** | Two-pass LLM: `TRANSCRIPT_SUMMARY_*` produces a per-video markdown summary (macro/vol/tickers); `IDEA_EXTRACTION_*` produces enriched `TradeIdea` records (`video_id`, `host`, `strikes`, `confidence`, `extracted_at`) |
| **Output** | `TradeIdea` rows in local SQLite + per-video summary markdown under `data/ideas/<date>/`; `daily_ideas` GSheet remains a deferred sink |
| **Key Distinction** | This captures **specific trade ideas** (e.g. "sell the May 45 DTE strangle on AAPL"), NOT general strategies. Strategies come from Module 0. |

**Extracted fields per idea:**
- Trader name, show, date
- Underlying ticker
- Strategy structure (matches an approved strategy type or flagged as new)
- Specific strikes / expiration mentioned
- Entry price / credit target
- Rationale / market context
- Confidence signals (how emphatic was the trader)
- Source URL + timestamp

**Two operational modes:**
```
Mode A: Live Transcript
  User plays YouTube video -> hits hotkey/script
  -> system captures audio via mic/loopback
  -> Whisper streaming transcription
  -> LLM extracts ideas in near-real-time
  -> writes to GSheet

Mode B: Record & Process
  User records segment (or downloads video)
  -> Whisper batch transcription
  -> LLM extracts ideas
  -> writes to GSheet
```

---

### Module 2: Portfolio Optimizer (The Edge)

This is the core intellectual property of the system. It answers:
**"Given my current portfolio and today's candidate ideas, which combination of
new trades (if any) most improves my portfolio's Greek profile?"**

#### 2A: Deterministic Optimization Engine (Code, Not LLM)

This is a pure algorithmic module. No LLM involved.

**Inputs:**
- Current portfolio positions + their Greeks (from broker API)
- Today's candidate ideas (from GSheet)
- Approved strategy configs (from `strategies.yaml`)
- Portfolio constraint bounds (from config)

**Algorithm:**
```
1. Fetch current portfolio state:
   - Per-position: delta, gamma, theta, vega, DTE, P&L
   - Portfolio aggregate: net delta, net gamma, net theta, net vega
   - Beta-weighted delta (to SPY)
   - BPR utilization

2. For each candidate idea:
   - Validate it matches an approved strategy type
   - Calculate theoretical Greeks for the proposed position
   - Project what portfolio Greeks would be WITH this position added

3. Enumerate feasible combinations:
   - Single additions, pairs, triples from the candidate set
   - Filter: only combos that pass ALL constraints

4. Score each feasible combination:
   - Delta improvement: how much closer to target (neutral or slight directional bias)
   - Gamma profile: prefer lower negative gamma or balanced gamma
   - Theta improvement: how much daily theta is added relative to risk
   - Diversification: underlying correlation, sector spread
   - Composite score = weighted sum (weights in config)

5. Rank and output top 3 combinations with full rationale
```

**Portfolio Constraints (hard limits, enforced in code):**
```yaml
constraints:
  beta_weighted_delta_pct: [-5.0, 5.0]    # % of NLV
  daily_theta_pct: [0.10, 0.30]           # % of NLV
  max_gamma_ratio: 1.5                     # |gamma/theta| cap
  max_vega_pct: 2.0                        # % of NLV
  max_bpr_utilization_pct: 50.0
  hard_bpr_cap_pct: 60.0                   # kill switch level
  max_single_underlying_pct: 15.0          # % of BPR
  max_sector_concentration_pct: 30.0
  min_positions: 3                          # diversification floor
  max_positions: 15                         # complexity cap
```

#### 2B: LLM Refinement Loop

After the deterministic engine ranks candidate combinations, an LLM agent reviews:

**What the LLM adds that code cannot:**
- Market regime interpretation (are we in a low-vol grind? a selloff? a vol expansion?)
- Earnings calendar awareness (should we avoid AAPL because earnings are in 3 days?)
- Correlation reasoning beyond simple beta (is TLT hedging our equity delta effectively?)
- Position adjustment suggestions (should we roll the existing SPY strangle before adding more SPY exposure?)
- Sanity check (does this combination make intuitive sense given the market environment?)

**How the loop works:**
```
Deterministic Engine outputs: Top 3 ranked combinations
       |
       v
LLM receives: ranked combos + current portfolio + market context
       |
       v
LLM outputs: {
  "selected_combo": 2,           // or "none" if all are poor
  "adjustments": [...],          // changes to existing positions first
  "reasoning": "...",            // auditable explanation
  "risk_flags": [...]            // anything the optimizer missed
}
       |
       v
IF LLM says "none" -> no trades today (this is fine and expected)
IF LLM selects a combo -> pass to Executor
IF LLM suggests adjustments -> those go to Executor too
```

**Key design principle:** The LLM can REJECT or MODIFY but cannot ADD ideas
that the deterministic engine didn't surface. The optimizer is the gatekeeper;
the LLM is the refinement layer.

---

### Module 3: Trade Executor

| Attribute | Detail |
|---|---|
| **Purpose** | Execute the approved trade plan via broker API |
| **Input** | Approved trade plan from Portfolio Optimizer |
| **Processing** | Size, construct multi-leg orders, route, monitor fills |
| **Output** | Fill confirmations, position updates, trade log entries |
| **Broker** | tastytrade (primary) or public.com (TBD based on API capabilities) |
| **Design** | Deterministic code. No LLM. |

**Broker evaluation criteria (tastytrade vs public.com):**
- Real-time Greeks per position via API?
- Portfolio-level Greeks available?
- Multi-leg order support?
- API rate limits / reliability?
- Commission structure?
- Margin/BPR calculation accessible via API?

**Execution flow:**
```
1. Receive approved trade plan (new trades + adjustments)
2. For each trade:
   a. Calculate position size (based on strategy allocation rules + BPR available)
   b. Construct order (legs, strikes, expiration)
   c. Get current mid-price
   d. Submit limit order at mid
   e. If not filled in N seconds, improve price by 1 cent, retry up to M times
   f. Log fill or timeout
3. Update positions sheet
4. Update portfolio snapshot
5. Send notification (Slack/Discord)
```

---

### Module 4: Position Manager (Ongoing)

Runs continuously during market hours. This is where the **optimization loop** delivers
compounding edge over time.

**Responsibilities:**
- Monitor all open positions against management rules (from strategies.yaml)
- Trigger profit-taking at target (e.g., 50% of max credit)
- Trigger loss management (close at 2x credit, or roll)
- Roll positions approaching DTE trigger
- Alert on constraint breaches (e.g., BPR utilization spiked due to market move)
- Daily end-of-day reconciliation and Greek snapshot

**The continuous refinement loop (future):**
```
Portfolio Optimizer (2A) runs daily or intraday
       |
       v
LLM Refinement (2B) interprets and suggests adjustments
       |
       v
Adjustments executed via Module 3
       |
       v
Results tracked (did the adjustment improve outcomes?)
       |
       v
Feed performance data back to refine optimizer weights
       |
       v
(loop continues - the system gets better over time)
```

---

## 6. Backtesting & Dry Run

### 6.1 Backtesting (Before Strategy Approval)

No strategy enters `strategies.yaml` without being backtested first.

**Data source evaluation:**

| Source | Cost | Coverage | Quality | MVP Viable? |
|---|---|---|---|---|
| **Polygon.io** | Free tier (5 API calls/min) or $29/mo | Full options chains, historical | Excellent | Yes (free tier for MVP) |
| **CBOE DataShop** | Paid | Gold standard for options | Best | No (cost) |
| **Yahoo Finance** | Free | Equity prices only, no options Greeks | Poor for options | No |
| **Tradier** | Free sandbox | Real-time + some historical options | Good | Maybe |
| **TD Ameritrade/Schwab API** | Free | Options chains, some historical | Good | Maybe |
| **ORATS** | Paid ($99/mo+) | Excellent historical options data | Excellent | Phase 2 |

**Recommendation for MVP:** Polygon.io free tier. Upgrade to paid if rate-limited.

**Backtest requirements:**
```
For each candidate strategy:
  1. Define parameters (from Module 0 extraction)
  2. Run against 2+ years of historical data
  3. Metrics required:
     - Win rate
     - Average P&L per trade
     - Max drawdown
     - Sharpe ratio
     - Average days in trade
     - Theta capture efficiency (actual P&L vs theoretical theta)
  4. Strategy approved only if metrics pass thresholds (configurable)
```

### 6.2 Dry Run (After Strategy Approval, Before Live)

Once a strategy is backtested and approved, it enters a **1-week dry run**:

```
Dry Run Mode:
- Portfolio Optimizer includes the new strategy in its scoring
- Executor generates orders but does NOT submit them
- Orders are logged as "dry_run" in trade_log sheet
- At end of week: compare simulated fills against actual market prices
- Human reviews dry run results
- If satisfactory: strategy flag set to dry_run_passed: true
- Strategy is now fully live
```

---

## 7. Data Model

### 7.1 Google Sheets

| Sheet | Purpose | Key Columns |
|---|---|---|
| `daily_ideas` | Module 1 output | id, date, trader, show, underlying, strategy_type, strikes, expiry, credit_target, rationale, confidence, source_url, timestamp, status |
| `positions` | Live position book | position_id, underlying, strategy_id, legs[], open_date, open_credit, current_value, greeks{d,g,t,v}, dte_remaining, pnl_pct, status |
| `portfolio_snapshot` | Daily Greek snapshot | date, net_delta, net_gamma, net_theta, net_vega, beta_wtd_delta, bpr_used_pct, nlv, daily_theta_pct, position_count |
| `trade_log` | All executions | trade_id, datetime, action(open/close/roll/adjust), underlying, legs[], fill_price, commission, strategy_id, optimizer_score, dry_run(bool) |
| `optimizer_decisions` | Audit trail | date, candidates_evaluated, combo_selected, reasoning, greeks_before, greeks_after, llm_commentary |
| `backtest_results` | Strategy validation | strategy_id, test_date, period, win_rate, avg_pnl, max_drawdown, sharpe, theta_efficiency, approved(bool) |

### 7.2 Local Files

| Path | Purpose |
|---|---|
| `data/transcripts/*.txt` | Historical tastytrade transcripts (bootstrap) |
| `data/audio/` | Recorded audio segments for batch processing |
| `config/strategies.yaml` | Approved strategy definitions (the source of truth) |
| `config/config.yaml` | System configuration (API keys, constraints, schedule) |
| `data/cache/greeks.db` | SQLite cache for Greeks/market data (avoid redundant API calls) |

---

## 8. Technology Stack

| Layer | Technology | Rationale |
|---|---|---|
| **Language** | Python 3.11+ | Ecosystem, finance libs, tastytrade SDK |
| **LLM** | OpenAI GPT-4o (or Claude) | Transcript extraction, refinement loop |
| **Transcription** | OpenAI Whisper API | Live + batch audio-to-text |
| **Audio Capture** | sounddevice / pyaudio | Capture system audio or mic for live mode |
| **Google Sheets** | gspread + google-auth | Human-auditable data layer |
| **Broker API** | tastytrade-sdk (or public.com TBD) | Order routing, position data, Greeks |
| **Options Pricing** | py_vollib / mibian | Independent Greek calculations |
| **Backtesting Data** | Polygon.io (free tier for MVP) | Historical options chains |
| **Optimization** | scipy.optimize / numpy | Deterministic combo scoring |
| **Scheduling** | APScheduler | Intraday monitoring, daily pipeline triggers |
| **Notifications** | Slack or Discord webhooks | Alerts, daily reports |
| **Storage** | Google Sheets (primary) + SQLite (cache) | Transparency + performance |

**Design principle:** Use deterministic code for all optimization and execution logic.
Use LLM only where human-like judgment adds value (transcript extraction, market
context interpretation, refinement review). Never use an LLM where a `for` loop
and a scoring function will do.

---

## 9. Phased Delivery Plan

### Phase 0: Foundation + Strategy Mining (Week 1-3)

**Goal:** Approved strategies in config, all APIs connected.

- [ ] Project scaffolding, CI, linting
- [ ] `config/config.yaml` system with YAML loading + validation
- [ ] Google Sheets API auth + read/write helpers
- [ ] Broker API auth (tastytrade sandbox initially)
  - [ ] Evaluate tastytrade vs public.com API capabilities
  - [ ] Document which provides: position Greeks, portfolio Greeks, BPR, multi-leg orders
- [ ] **Module 0: Strategy Miner**
  - [ ] Bootstrap `data/transcripts/` with 10+ transcripts
  - [ ] LLM extraction pipeline (transcript -> candidate strategies)
  - [ ] Human review interface (CLI or simple web UI)
  - [ ] Output: `config/strategies.yaml` with 3-4 approved strategies
- [ ] **Backtesting framework**
  - [ ] Polygon.io integration for historical options data
  - [ ] Backtest runner for each approved strategy
  - [ ] Results logged to `backtest_results` sheet
  - [ ] Strategies marked `backtest_approved: true`

### Phase 1: Live Idea Capture (Week 4-5)

**Goal:** Daily ideas flowing into GSheet from live content.

- [ ] **Module 1: Live Idea Scraper**
  - [ ] Audio capture (system audio loopback or mic input)
  - [ ] Whisper integration (streaming mode for live, batch mode for recorded)
  - [ ] LLM extraction prompt: transcript -> structured trade ideas
  - [ ] GSheet writer: ideas -> `daily_ideas` sheet
  - [ ] CLI script: `vol_crush capture --mode live` / `vol_crush capture --mode record`
  - [ ] Test with 5+ real tastytrade segments

### Phase 2: Portfolio Optimizer (Week 6-8)

**Goal:** Given ideas + current portfolio, output optimal trade plan.

- [ ] **Module 2A: Deterministic Engine**
  - [ ] Broker API: fetch current positions + Greeks
  - [ ] Greek aggregation (portfolio-level delta, gamma, theta, vega)
  - [ ] Beta-weighted delta calculation
  - [ ] Candidate idea Greek estimation (from options chain data)
  - [ ] Combination enumeration + constraint filtering
  - [ ] Scoring function (configurable weights)
  - [ ] Output: ranked feasible combinations
- [ ] **Module 2B: LLM Refinement**
  - [ ] Market context gathering (VIX level, regime, earnings calendar)
  - [ ] LLM prompt: ranked combos + context -> selection + adjustments
  - [ ] Audit logging to `optimizer_decisions` sheet
- [ ] **Dry run mode**
  - [ ] Optimizer runs, generates orders, logs as dry_run
  - [ ] 1-week dry run for each strategy before going live

### Phase 3: Execution + Position Management (Week 9-11)

**Goal:** Trades executing, positions managed automatically.

- [ ] **Module 3: Executor**
  - [ ] Order construction (multi-leg options)
  - [ ] Position sizing (BPR-aware, strategy allocation rules)
  - [ ] Limit order with price improvement logic
  - [ ] Fill monitoring + logging
  - [ ] GSheet updates (positions, trade_log, portfolio_snapshot)
- [ ] **Module 4: Position Manager**
  - [ ] Profit target monitoring (close at X% of max credit)
  - [ ] Loss management (close or roll at threshold)
  - [ ] DTE-based roll triggers
  - [ ] Constraint breach alerts
  - [ ] End-of-day reconciliation + snapshot

### Phase 4: Go Live + Iterate (Week 12+)

**Goal:** Full pipeline running on real capital.

- [ ] Final broker selection (tastytrade vs public.com)
- [ ] Live deployment with small capital
- [ ] Daily monitoring dashboard / report
- [ ] Performance tracking (actual vs backtest expectations)
- [ ] Optimizer weight tuning based on real results
- [ ] **Begin refinement loop**: performance data feeds back into optimizer scoring
- [ ] Expand Module 1 sources (X, other content feeds)

---

## 10. Risk & Mitigation

| Risk | Severity | Mitigation |
|---|---|---|
| **Bad strategy extraction from transcripts** | High | Human approval gate (Module 0); strategies backtested before use |
| **Optimizer selects poor combination** | High | LLM refinement layer; hard constraint limits; dry run validation |
| **Greek calculation errors** | Critical | Cross-validate own calcs against broker-reported Greeks; unit tests |
| **Over-leveraging** | Critical | Hard BPR cap in code (not just config); kill switch at 60% BPR |
| **Broker API outage** | Medium | Queue-based execution; manual fallback; alerts |
| **LLM hallucination in refinement** | Medium | LLM can only reject/modify, never add new ideas; deterministic engine is gatekeeper |
| **Audio capture quality** | Low | Fallback to record-and-process mode; Whisper handles noise well |
| **Live execution errors** | Critical | Dry run validation before any strategy goes live; position size limits |

---

## 11. Success Metrics

| Metric | Target | Measurement |
|---|---|---|
| Idea capture rate | 90%+ of actionable ideas from watched content | Manual audit weekly |
| Portfolio theta | 0.1-0.3% of NLV daily | `portfolio_snapshot` sheet |
| Beta-weighted delta | Within +/-5% of NLV, 95% of days | `portfolio_snapshot` sheet |
| Trade win rate | 70%+ closed at profit target | `trade_log` analysis |
| Optimizer accuracy | Selected combos outperform random selection by 2x+ | Backtest comparison |
| Dry run fidelity | Simulated fills within 5% of actual market prices | `trade_log` dry_run entries |
| Annual ROI | 15-25% on deployed capital | Account P&L |
| System uptime | 99%+ during market hours | Monitoring |

---

## 12. Decisions Made

Based on initial product discussion:

| Decision | Choice | Rationale |
|---|---|---|
| Paper vs Live | Go live ASAP (after backtest + 1-week dry run) | Paper trading delays learning; small capital limits risk |
| Account scope | Single account | Simplicity for MVP |
| LLM vs Code | Code for all deterministic decisions; LLM only for judgment calls | Reliability, speed, auditability |
| Broker | TBD (tastytrade vs public.com) | Evaluate API capabilities in Phase 0 |
| MCP servers | Build from ground up | Need tight control over optimization loop |
| Backtest data | Polygon.io free tier for MVP | Free, good coverage, upgrade path available |
| Content bootstrap | Manual transcripts in `data/transcripts/` | Avoids scraping complexity for initial strategy mining |
| Strategy updates | Rare, human-approved | Stability > novelty; strategies are the foundation |

---

## 13. Open Questions (Remaining)

1. **public.com API**: Does it expose per-position Greeks and portfolio-level Greeks via API? Need to evaluate.
2. **Optimizer weights**: Initial weights for delta/gamma/theta scoring - start equal and tune, or use tastytrade heuristics (theta-dominant)?
3. **Intraday frequency**: How often should the position manager check positions? Every 15 min? Every hour?
4. **Earnings handling**: Auto-close positions before earnings, or is that an LLM refinement decision?
5. **Kill switch**: Should there be a hard stop (close everything) if portfolio drawdown exceeds X%?

---

## 14. Immediate Next Steps

1. **Create `data/transcripts/` folder** and add first 10 transcripts
2. **Evaluate broker APIs** - test tastytrade-sdk and public.com for Greek data availability
3. **Build Module 0** - strategy extraction from transcripts
4. **Set up Google Sheet** with the 6 tabs defined in the data model
5. **Build backtesting framework** with Polygon.io integration
6. **Run first backtest** on extracted strategies

---

## Appendix A: Example Daily Flow

```
8:00 AM  - System starts, fetches current portfolio from broker
         - Portfolio: 3 positions (SPY strangle, IWM iron condor, GLD strangle)
         - Net delta: +2.1% NLV, theta: 0.15% NLV, BPR: 35%

9:00 AM  - User watches tastytrade morning show
         - Hits capture script
         - Module 1 extracts 5 ideas:
           1. AAPL 45-DTE short strangle
           2. TSLA iron condor
           3. TLT short put
           4. META short strangle
           5. Roll SPY strangle out to next month

9:30 AM  - Market opens
         - Portfolio Optimizer runs:
           - Evaluates all 15 combinations (5 choose 1, 2, 3)
           - Filters to 4 feasible combos (others breach constraints)
           - Scores: Combo {TLT put + META strangle} scores highest
             (adds theta, TLT hedge reduces beta-weighted delta)
           - LLM reviews: "Agree with TLT + META. Note META earnings in 2 weeks,
             suggest shorter DTE. Also agree with rolling SPY per idea #5."
           - Final plan: Open TLT put, Open META strangle (30 DTE), Roll SPY

9:35 AM  - Executor routes 3 orders
         - Fills logged, positions updated
         - New portfolio: 5 positions, delta: +0.8% NLV, theta: 0.22% NLV

2:00 PM  - Position Manager checks: SPY strangle hit 50% profit target
         - Auto-closes SPY strangle
         - Logs trade, updates snapshot

4:15 PM  - EOD reconciliation
         - Portfolio snapshot saved
         - Daily report sent to Slack
```

---

*This is a living document. Version history tracked in Git.*
