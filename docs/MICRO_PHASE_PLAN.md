# Kamandal Micro-Phase Delivery Plan

Execution source of truth for the current dry-run implementation.

## Status Key

- `[x]` complete
- `[~]` complete at high level with intentional MVP simplifications
- `[ ]` deferred

## Delivery Summary

- Goal delivered: standalone local-first dry-run pipeline from ideas to optimizer decisions to pending orders.
- Runtime dependencies on sibling repos: none.
- Reused sibling assets during implementation: `public_api_trading_v3/gds_history.db`, `public_api_trading_v3/analysis/extracted_gds_data.json`, and Bhiksha-style env/config patterns.
- Live broker execution: intentionally deferred.
- Follow-on extension delivered: source-adapter based idea acquisition for transcripts, YouTube, RSS, and generic web pages.

## Micro-Phases

### MP0 Repo hardening and execution plan bootstrap

- `[x]` Added this micro-phase plan document.
- `[x]` Stabilized test bootstrap by making the OpenAI import lazy in [llm.py](/Users/suman/kg_env/projects/kamandal/vol_crush/integrations/llm.py).
- `[x]` Added canonical runtime entrypoints:
  - `python -m vol_crush.strategy_miner`
  - `python -m vol_crush.idea_scraper`
  - `python -m vol_crush.idea_sources`
  - `python -m vol_crush.integrations.fixtures`
  - `python -m vol_crush.optimizer`
  - `python -m vol_crush.executor`
  - `python -m vol_crush.position_manager`
  - `python -m vol_crush.backtester`
  - `python -m vol_crush.main`

### MP1 Core domain and interface expansion

- `[x]` Expanded shared models in [models.py](/Users/suman/kg_env/projects/kamandal/vol_crush/core/models.py).
- `[x]` Added normalized status enums for ideas, positions, backtests, execution mode, regimes, and plan decisions.
- `[x]` Added shared runtime interfaces in [interfaces.py](/Users/suman/kg_env/projects/kamandal/vol_crush/core/interfaces.py).

### MP2 Local-first persistence layer

- `[x]` Implemented SQLite-backed `LocalStore` in [storage.py](/Users/suman/kg_env/projects/kamandal/vol_crush/integrations/storage.py).
- `[x]` Added JSON audit mirrors under `data/audit/`.
- `[x]` Rewired the idea scraper to persist through the storage backend.
- `[x]` Added raw source-document persistence for multi-source idea acquisition.

### MP3 Fixture and imported data pipeline

- `[x]` Implemented fixture builder/provider in [fixtures.py](/Users/suman/kg_env/projects/kamandal/vol_crush/integrations/fixtures.py).
- `[x]` Imports:
  - `public_api_trading_v3/gds_history.db`
  - `public_api_trading_v3/analysis/extracted_gds_data.json`
- `[~]` Public internet seeding is implemented for underlying context only.
  - Option-chain fixture content still comes from imported/local sources.
- `[x]` Normalized fixture outputs:
  - `data/fixtures/fixture_bundle.json`
  - `data/fixtures/replay_trades.json`

### MP3A Source acquisition extension

- `[x]` Added source adapters under [idea_sources](/Users/suman/kg_env/projects/kamandal/vol_crush/idea_sources).
- `[x]` Implemented adapters for:
  - local transcript directories
  - YouTube channel feeds with best-effort transcript extraction
  - RSS feeds
  - generic web pages
- `[x]` Added raw-document dedupe by fingerprint before extraction.
- `[x]` Added idea dedupe before storing newly extracted trade ideas.
- `[x]` Added CLI entrypoint: `python -m vol_crush.idea_sources ...`

### MP4 Strategy and regime policy system

- `[x]` Added `portfolio.regimes` to [config.example.yaml](/Users/suman/kg_env/projects/kamandal/config/config.example.yaml).
- `[x]` Implemented `ConfigRegimeEvaluator` in [service.py](/Users/suman/kg_env/projects/kamandal/vol_crush/optimizer/service.py).
- `[x]` Default regimes cover `high_iv`, `normal_iv`, `low_iv`, and `event_risk`.

### MP5 Deterministic portfolio optimizer

- `[x]` Validates ideas against approved strategies, underlyings, fixture availability, IV regime bounds, and event risk.
- `[x]` Expands candidates into estimated positions with Greeks, BPR, and legs.
- `[x]` Enumerates singles, pairs, and triples.
- `[x]` Enforces hard constraints before execution.
- `[x]` Ranks combos on delta improvement, gamma/theta profile, theta improvement, diversification, and regime fit.
- `[x]` Emits explicit `no_trade` plans when nothing passes or nothing improves the portfolio.

### MP6 Pending executor and position manager

- `[x]` Pending executor implemented in [service.py](/Users/suman/kg_env/projects/kamandal/vol_crush/executor/service.py).
- `[x]` Position manager implemented in [service.py](/Users/suman/kg_env/projects/kamandal/vol_crush/position_manager/service.py).
- `[~]` Position sizing is intentionally heuristic for MVP.
- `[x]` Pending orders support open, close, and roll recommendations.

### MP7 Basic backtest and replay gate

- `[x]` Replay/backtest service implemented in [service.py](/Users/suman/kg_env/projects/kamandal/vol_crush/backtester/service.py).
- `[x]` Computes win rate, P/L, drawdown, average days in trade, and theta-capture proxy.
- `[x]` Writes results to the local store and updates `backtest_approved` in `strategies.yaml`.
- `[~]` Historical replay currently uses imported replay fixtures, not full historical option-chain reconstruction.

### MP8 CLI orchestration and daily pipeline

- `[x]` Added end-to-end dry-run orchestrator in [main.py](/Users/suman/kg_env/projects/kamandal/vol_crush/main.py).
- `[x]` Verified the dry-run pipeline executes end to end with no live APIs.
- `[x]` Added optional source-fetch stage via `--fetch-sources`.
- `[x]` Pipeline flow:
  1. optionally fetch source content and extract ideas
  2. refresh fixtures
  3. refresh replay trades
  4. optional backtest gate
  5. build optimizer plan
  6. emit pending orders
  7. evaluate open positions

### MP9 Validation, docs, and operator readiness

- `[x]` Expanded the test suite to 43 passing tests.
- `[x]` Added coverage for storage, fixtures, optimizer behavior, pending execution, position management, and replay evaluation.
- `[x]` Updated [README.md](/Users/suman/kg_env/projects/kamandal/README.md) to match the current implementation.

## Verification

Executed successfully during implementation:

- `.venv/bin/python -m pytest -q`
- `.venv/bin/python -m vol_crush.idea_sources --source transcripts --extract-ideas`
- `.venv/bin/python -m vol_crush.integrations.fixtures`
- `.venv/bin/python -m vol_crush.main --skip-backtest --fetch-sources transcripts`

Most recent result:

- Tests: `43 passed`
- Source fetch: completed for transcript-source mode and persisted raw documents
- Fixture build: completed, wrote fixture and replay artifacts under `data/fixtures/`
- Daily pipeline: completed in dry-run mode and emitted `no_trade` with the current empty strategy set

## Known MVP Gaps

- Live tastytrade/public/schwab broker execution is not implemented.
- Google Sheets is not yet wired; local SQLite plus JSON audit files are the primary store.
- Greek and BPR estimation are heuristic when no broker-grade chain data is available.
- Replay backtesting is a pragmatic gate, not a full institutional historical options simulator.
- YouTube transcript extraction is best-effort and depends on caption availability in the public watch page.
