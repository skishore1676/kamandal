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

### MP11 Pluggable transcript providers + retry CLI

- `[x]` New `vol_crush/transcript_providers/` module — reusable outside of `idea_sources`. Exposes `TranscriptProvider` Protocol, `TranscriptFetch` dataclass, `ProviderChain`, and a name→factory `PROVIDER_REGISTRY` with a `register_provider()` escape hatch.
- `[x]` Built-in providers: `YouTubeCaptionProvider` (free, caption-based) and `GroqWhisperProvider` (paid audio fallback — yt-dlp → ffmpeg chunk → Groq `whisper-large-v3-turbo`, off by default, gated on `GROQ_API_KEY`).
- `[x]` Config-driven chain via `idea_sources.transcripts.providers` list with per-entry `enabled` and provider-specific options.
- `[x]` `YouTubeChannelAdapter` now accepts a chain; default wiring builds one from config so existing callers keep working.
- `[x]` New CLI `python -m vol_crush.idea_sources.retry_transcripts` re-runs the chain against raw documents still missing a transcript, scoped by a `[min_age_hours, max_age_hours]` window (defaults 20h / 168h). On success it re-archives the transcript, writes a summary, and re-runs idea extraction.
- `[x]` Tests: 15 new cases covering each provider, chain ordering + error tolerance, config-driven registry, and retry service filtering / dry-run / end-to-end recovery (mocked). Suite is 121 green.

### MP10 LLM provider scaffold and YouTube idea pipeline upgrade

- `[x]` Refactored `vol_crush/integrations/llm.py` into a provider-agnostic OpenAI-compatible client (`openai` and `openrouter`) with `build_llm_client(config)` factory.
- `[x]` Added primary → fallback model failover so transient 429/5xx/model-not-found errors transparently retry with `llm.fallback_model` (env: `VOL_CRUSH_LLM_MODEL_BACKUP`).
- `[x]` New `llm:` config section + env overrides (`VOL_CRUSH_LLM_*`); legacy `openai:` section kept as a back-compat fallback. Audio/Whisper still requires `provider=openai`.
- `[x]` Swapped YouTube transcript scraping for `youtube-transcript-api`; added macOS certifi-backed SSL context, browser User-Agent, and exponential-backoff retry for transient 4xx/5xx in `vol_crush/idea_sources/utils.py`.
- `[x]` New `transcript_archive` writes every fetched transcript + sidecar JSON to `data/transcripts/archive/<source>/<date>/`; configurable retention purge runs at the start of each fetch (default 14 days).
- `[x]` Added `TRANSCRIPT_SUMMARY_*` prompt + `summarize_transcript()` pass that produces per-video markdown under `data/ideas/<date>/<video_id>_summary.md` (macro view, vol view, tickers with bias, notable quotes).
- `[x]` Enriched `TradeIdea` schema with `video_id`, `host`, `strikes: list[float]`, `extracted_at`. Storage is JSON-payload based, so no SQL migration was required.
- `[x]` Optional title pre-filter (`idea_sources.youtube.title_include_keywords` / `title_exclude_keywords`) skips transcript fetch + LLM cost for non-matching videos.
- `[x]` New `vol_crush.llm_compare` CLI replays an archived transcript through N models and writes side-by-side `.json` + `.md` reports under `data/llm_comparisons/<date>/`.
- `[x]` Test suite grew to 106 passing tests; new coverage for title filter, archive purge, summary render, HTTP retry semantics, and the comparison harness end-to-end (mocked LLM).

## Verification

Executed successfully during implementation:

- `.venv/bin/python -m pytest -q`
- `.venv/bin/python -m vol_crush.idea_sources --source transcripts --extract-ideas`
- `.venv/bin/python -m vol_crush.integrations.fixtures`
- `.venv/bin/python -m vol_crush.main --skip-backtest --fetch-sources transcripts`

Most recent result (2026-04-15):

- Tests: `106 passed`
- YouTube fetch: archived transcripts to `data/transcripts/archive/youtube/<date>/` and produced per-video summaries under `data/ideas/<date>/`
- LLM comparison harness: ran `gemma-4-31b-it:free`, `deepseek/deepseek-v3.2`, and `anthropic/claude-haiku-4.5` against the same transcript and wrote a side-by-side `.md` + `.json` report
- Source fetch: completed for transcript-source mode and persisted raw documents
- Fixture build: completed, wrote fixture and replay artifacts under `data/fixtures/`
- Daily pipeline: completed in dry-run mode and emitted `no_trade` with the current empty strategy set

## Known MVP Gaps

- Live tastytrade/public/schwab broker execution is not implemented.
- Google Sheets is not yet wired; local SQLite plus JSON audit files are the primary store.
- Greek and BPR estimation are heuristic when no broker-grade chain data is available.
- Replay backtesting is a pragmatic gate, not a full institutional historical options simulator.
- YouTube transcript extraction uses [youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api) and still depends on caption availability. Live streams that disable captions (e.g. tastylive's daily live show) currently archive only the video description; an audio-Whisper fallback is not yet wired.
