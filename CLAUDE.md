# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Naming Note

The repo is `kamandal` but the runtime Python package is still named `vol_crush`. All CLI entrypoints and imports use `vol_crush.*`.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config/config.example.yaml config/config.yaml   # optional override
```

## Common Commands

```bash
# Daily dry-run end-to-end (fixtures → optimizer → executor → position manager)
python -m vol_crush.main --skip-backtest
python -m vol_crush.main --skip-backtest --fetch-sources transcripts

# Individual pipeline modules
python -m vol_crush.strategy_miner                           # one-time LLM strategy extraction from transcripts
python -m vol_crush.integrations.fixtures                    # build local market/replay fixtures
python -m vol_crush.idea_sources --source transcripts
python -m vol_crush.idea_sources --source youtube --channel-id ID --extract-ideas
python -m vol_crush.idea_sources --source rss --feed-url URL --extract-ideas
python -m vol_crush.idea_sources --source web --url URL --extract-ideas
python -m vol_crush.idea_scraper --mode transcript --file data/transcripts/file.txt
python -m vol_crush.optimizer                                # deterministic Greek-aware combo scoring
python -m vol_crush.executor                                 # emit pending orders
python -m vol_crush.position_manager                         # close/roll/adjust recommendations (whole-group only)
python -m vol_crush.backtester                               # replay gate
python -m vol_crush.portfolio_sync --broker public --show-groups   # pretty-print grouped view

# Tests / formatting
pytest
pytest tests/test_services.py -v
pytest tests/test_services.py::test_name -v
black vol_crush tests
ruff check vol_crush tests
```

## Architecture

Modules communicate via a shared `StorageBackend` (SQLite at `data/kamandal.db`) and JSON fixtures, not in-process calls. `vol_crush/main.py` orchestrates the daily pipeline in order.

```
strategy_miner   (one-time)   transcripts → LLM distill → strategy candidates (human review)
idea_sources     (adapters)   youtube/rss/web/transcripts → RawSourceDocument → LLM → TradeIdea
integrations/fixtures         sibling repo data (public_api_trading_v3) + public seeds → fixture_bundle.json + replay_trades.json
backtester                    replays ReplayTrade set against approved strategies (approval gate)
portfolio_sync                pulls live Public portfolio → raw BrokerPositionLeg → position_grouping → grouped Positions
position_grouping             deterministic classifier: raw legs → strategy bundles (iron_condor, vertical, strangle, ...)
optimizer                     combo-scores TradeIdeas against PortfolioSnapshot (group-level) + RegimePolicy → TradePlan
executor                      sizes TradePlan → PendingOrder, stamps Public orderId UUID as durable group anchor
position_manager              evaluates auto-managed groups → whole-group close/roll actions; refuses manual_review_required
```

### Position grouping layer (critical safety invariant)

Kamandal separates the broker's leg-level view from the trading brain's group-level view:

- **Raw legs** (`BrokerPositionLeg` in `broker_position_legs` SQLite table) — verbatim audit floor of what Public reports. Never read by optimizer or position_manager.
- **Grouped Positions** (`Position` in `positions` table) — one row per strategy bundle (iron condor, vertical spread, strangle, naked short put, ...). This is what the trading brain reasons about: risk, BPR, diversification, close/roll decisions.

`vol_crush/position_grouping/service.py` is the deterministic classifier. Ordering is most-specific-first: iron condor → jade lizard → straddle → strangle → vertical → single-leg → calendar → orphan/unknown. Anything unclassified becomes `StrategyType.UNKNOWN_COMPLEX` or `ORPHAN_LEG` with `management_status=MANUAL_REVIEW_REQUIRED`, and downstream services refuse to auto-manage it.

For Kamandal-opened trades we don't re-infer the bundle: the executor stamps a client-supplied Public `orderId` UUID on the `PendingOrder.broker_order_id` **before** submission (`PublicBrokerAdapter._ensure_group_anchor`). On the next sync, `position_grouping.reconcile_with_known_orders` matches the live legs to that anchor and produces a `source=kamandal_order` Position that inherits the original `strategy_id` and preflight BPR. Public API doesn't preserve strategy identity in `portfolio/v2` — the UUID is what lets us anyway.

Safety rules enforced in code (not config):
- `position_manager` refuses to emit any action when `management_status != AUTO`.
- `CLOSE` orders must carry every leg of the source group — hard assertion in `_assert_full_group_close`.
- Optimizer's `position_count` is the count of auto-managed groups, **not** legs; manual-review groups still contribute Greeks and BPR to the aggregate but don't count toward `max_positions` or diversification.
- `portfolio.constraints.max_orphan_legs` (default 0) blocks new opens while ungrouped short or unknown-complex legs exist.

### Layering

- `vol_crush/core/` — typed domain (`models.py`: `Position`, `BrokerPositionLeg`, `PositionSource`, `GroupConfidence`, `ManagementStatus`, `StrategyType`), runtime `Protocol`s (`interfaces.py`: `StorageBackend`, `MarketDataProvider`, `RegimeEvaluator`, `BrokerAdapter`), config loader, logging. Other modules depend only on `core` interfaces, not concrete integrations.
- `vol_crush/integrations/` — concrete adapters: `storage.py` (SQLite + audit JSON), `fixtures.py` (`FixtureMarketDataProvider`, bundle builder), `llm.py` (lazy OpenAI), `public_broker.py` (Public broker client + UUID anchor stamping).
- `vol_crush/position_grouping/` — pure classifier + BPR formulas; no storage or network. Imported by `portfolio_sync` and consumed transparently by the optimizer and position_manager via grouped `Position` objects.
- Each pipeline module is a `service.py` (library entrypoint — e.g. `build_trade_plan`, `execute_latest_plan`, `evaluate_positions`, `run_backtests`, `sync_public_portfolio`) plus a `__main__.py` CLI shim so `python -m vol_crush.<module>` and the orchestrator both call the same code path.

## Config Model

`load_config()` merges `config.yaml` over `config.example.yaml` (deep merge), then applies `VOL_CRUSH_*` env overrides. The example file is the authoritative schema reference — read it when adding a new knob.

- `broker.active`: `tastytrade` | `public` | `schwab`. Currently only `public` has an implementation; tastytrade/schwab stubs are placeholders.
- `execution.mode`: `dry_run` (preflight only) | `pending` (write PendingOrder, don't submit) | `live` (submit). All three write audit records; only `live` hits the broker.
- Live-mode safety gates (enforced in optimizer/executor, not config): strategies must have both `backtest_approved` and `dry_run_passed` before `build_trade_plan` will emit candidates under `mode=live` (unapproved templates still run in `dry_run`/`pending` to collect evidence); `execution.max_contracts_per_order` hard-caps sizing per order (defaults to `1` in live mode if unset).
- `portfolio.constraints` are hard limits enforced in optimizer code (beta-weighted delta ±5% NLV, daily theta 0.10–0.30% NLV, |gamma/theta| < 1.5, BPR util < 50%, single underlying < 15% BPR, `max_orphan_legs` guard for ungrouped shorts).
- `portfolio.regimes` (`high_iv` | `normal_iv` | `low_iv` | `event_risk`) drive which structures the optimizer prefers/avoids; `event_risk` rejects new exposure.
- `data_sources.fixtures.import_gds_history_db` and `import_gds_analysis_json` point into the sibling `public_api_trading_v3` repo — fixtures builder reads those paths directly.

### Strategy config (two-file model)

Strategies are NOT defined per-ticker. Two files combine at resolution time:

- **`config/strategy_templates.yaml`** — structure-level templates (put_spread, iron_condor, short_put, ...). Each template defines entry filters, management rules, regime eligibility, and earnings avoidance. Underlying-agnostic.
- **`config/underlying_profiles.yaml`** — universe groupings (index_etf, bond_etf, commodity_etf). Each profile lists symbols, allowed structures, and allocation caps (max_bpr_pct, max_positions).

At runtime: `resolve_all_strategies(templates, profiles)` produces one `Strategy` per eligible (template, profile) pair. The optimizer and position_manager consume these resolved `Strategy` objects. `config/strategies.yaml` is legacy — kept for backward compatibility but empty.

## Data Layout

- `data/kamandal.db` — SQLite `StorageBackend` (ideas, plans, orders, positions, snapshots, backtests). `trade_ideas` and `raw_documents` tables store the entire dataclass as a JSON `payload` column, so adding fields to `TradeIdea` / `RawSourceDocument` requires no SQL migration (only updates to `to_dict` / `from_dict`).
- `data/fixtures/fixture_bundle.json`, `data/fixtures/replay_trades.json` — regenerated each daily run.
- `data/audit/` — JSON audit trail for every plan, order, and management action.
- `data/transcripts/` — source inputs for `strategy_miner` and `idea_scraper` (human-curated).
- `data/transcripts/archive/<source>/<date>/<video_id>.{txt,meta.json}` — on-disk copy of every fetched transcript + sidecar metadata; written by `vol_crush/idea_sources/transcript_archive.py`. Purged at the start of each fetch run once files exceed `idea_sources.transcripts_archive.retention_days` (default 14). Also consumed by `vol_crush.llm_compare` to replay a transcript through multiple models.
- `data/ideas/<date>/<video_id>_summary.md` — LLM-generated markdown summaries (one per transcript) written by `vol_crush/idea_scraper/summary_archive.py`. These are a human-scan layer that runs *before* structured idea extraction so you still get macro/ticker/vol commentary even when no actionable trade is extracted.
- `data/llm_comparisons/<date>/<video_id>_compare.{json,md}` — side-by-side model comparison reports from `python -m vol_crush.llm_compare`.
- `data/audio/` — source inputs for live audio capture (Whisper path, OpenAI provider only).
- `data/cache/public_*.json` — Public broker session/account cache.

## LLM provider + idea extraction

- `LLMClient` in `vol_crush/integrations/llm.py` is OpenAI-compatible but supports OpenRouter (auto base URL swap) and primary → fallback failover. `build_llm_client(config)` reads the `llm:` config section; legacy `openai:` acts as a fallback for back-compat. Env overrides: `VOL_CRUSH_LLM_PROVIDER`, `VOL_CRUSH_LLM_API_KEY` (or `OPENROUTER_API_KEY`), `VOL_CRUSH_LLM_MODEL`, `VOL_CRUSH_LLM_MODEL_BACKUP`.
- `vol_crush/idea_scraper/prompts.py` holds two prompts: `IDEA_EXTRACTION_*` (structured JSON with ticker/strategy/strikes/confidence/host/timestamp) and `TRANSCRIPT_SUMMARY_*` (macro view, vol view, tickers with bias, notable quotes). Both run against every new YouTube document in `run_source_fetch`.
- `TradeIdea` carries `video_id`, `host`, `strikes: list[float]`, `extracted_at` alongside legacy `trader_name` / `source_url` / `source_timestamp` / `confidence` fields.
- YouTube transcripts are fetched via `youtube-transcript-api` (not HTML scraping). Live streams that disable captions (e.g. tastylive's daily live show) will archive only the video description — `metadata.has_transcript` flags this.
- `idea_sources.youtube.title_include_keywords` / `title_exclude_keywords` are a pre-LLM filter: videos whose titles don't match are skipped before transcript fetch to save cost.
- `vol_crush/idea_sources/utils.py::fetch_url` wraps urllib with exponential-backoff retry (3 attempts, 1s/2s/4s) on 404/408/425/429/5xx/timeout — needed because YouTube RSS endpoints return 404 intermittently even for valid channels.

### LLM comparison harness

```bash
python -m vol_crush.llm_compare \
  --video-id Z7Z2fedV1TQ \
  --models "anthropic/claude-sonnet-4.5,anthropic/claude-haiku-4.5,deepseek/deepseek-v3.2"
```

Reads the archived transcript for `--video-id` (from `data/transcripts/archive/`), runs both the summary and extraction prompts against each model, and writes a `.json` + `.md` side-by-side report under `data/llm_comparisons/<date>/`. Uses the same provider + api_key as the live pipeline.

## Testing Notes

- OpenAI is imported lazily in `integrations/llm.py` so tests run without the API key.
- `tests/test_public_broker.py` and `tests/test_public_portfolio_sync.py` exercise the Public adapter with mocked HTTP; don't hit the real API.
- Pipeline modules are tested by calling the `service.py` functions directly against an in-memory/temp `StorageBackend`, not via the `__main__` CLI.

## References

- `docs/PROJECT_DOCUMENT.md` — full product/architecture spec.
- `docs/MICRO_PHASE_PLAN.md` — micro-phase execution status, MVP simplifications, known gaps.
- `docs/BROKER_EVALUATION.md` — broker selection rationale.
