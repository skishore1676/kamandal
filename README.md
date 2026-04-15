# Kamandal

> **A hedge fund in a box for tastytrade-style premium selling.**

Kamandal is an AI-powered options portfolio management system for tastytrade-style
premium selling. The current implementation is a local-first dry-run stack that can
mine strategies, capture ideas from multiple sources, build market fixtures,
optimize portfolio Greeks, emit pending orders, and run a basic replay gate
without live broker connectivity.

## The Big Idea

Most retail options traders think **trade by trade**. Kamandal thinks **portfolio by
portfolio**. Every day, the system ingests new trade ideas and selects the
**combination of ideas that best improves the portfolio's delta, gamma, and theta
profile**. That Greek-level portfolio optimization is where the edge lives.

## Architecture

```text
Module 0: Strategy Miner ──── one-time: transcripts → LLM → review → config
Module 1: Idea Intake ─────── source adapters → raw docs → LLM summary + extraction → local store
                              + on-disk transcript archive (14-day retention) and per-video markdown summaries
Fixtures ──────────────────── local bundle built from sibling repo data + public seeds
Module 2: Portfolio Optimizer ─ deterministic Greek-aware combo scoring
Module 3: Pending Executor ── size and emit dry-run/pending orders
Module 4: Position Manager ── recommend closes/rolls/adjustments
Backtester ────────────────── replay gate from imported fixture trades
LLM Compare (CLI) ──────────── replay one archived transcript through N models for side-by-side review
```

## Quick Start

```bash
cd kamandal
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Optional: bootstrap local env vars
cp .env.example .env

# Optional: copy config template for overrides
cp config/config.example.yaml config/config.yaml

# Runtime package note: the internal Python package is still named `vol_crush`
# for now, so the CLI entrypoints below still use that module path.

# One-time strategy mining
python -m vol_crush.strategy_miner

# Build local market/replay fixtures
python -m vol_crush.integrations.fixtures

# Fetch new source content into the raw document store
python -m vol_crush.idea_sources --source transcripts
python -m vol_crush.idea_sources --source youtube --channel-id YOUR_CHANNEL_ID --extract-ideas
python -m vol_crush.idea_sources --source rss --feed-url https://example.com/feed.xml --extract-ideas
python -m vol_crush.idea_sources --source web --url https://example.com/post --extract-ideas

# Replay/backtest approved strategies
python -m vol_crush.backtester

# Capture daily ideas directly from a transcript/audio file
python -m vol_crush.idea_scraper --mode transcript --file data/transcripts/your_file.txt

# Run the deterministic optimizer
python -m vol_crush.optimizer

# Emit pending orders and management actions
python -m vol_crush.executor
python -m vol_crush.position_manager

# Or run the daily dry-run pipeline end to end
python -m vol_crush.main --skip-backtest

# Or pull source content first, then run the daily dry-run pipeline
python -m vol_crush.main --skip-backtest --fetch-sources transcripts

# Compare multiple LLMs against a previously archived YouTube transcript
python -m vol_crush.llm_compare --video-id Z7Z2fedV1TQ \
    --models "anthropic/claude-haiku-4.5,deepseek/deepseek-v3.2,openai/gpt-4o"
```

If `broker.active` is set to `public`, `execution.mode=dry_run` or `pending`
will use Public preflight/mock-style checks without placing live orders.
`execution.mode=live` is the mode that submits broker orders.

## Project Structure

```text
kamandal/
├── config/
│   ├── config.example.yaml
│   ├── strategy_templates.yaml    # structure-level strategy definitions
│   ├── underlying_profiles.yaml   # universe groupings + allocation caps
│   └── strategies.yaml            # legacy (deprecated, kept for compat)
├── data/
│   ├── transcripts/             # human-curated source inputs
│   │   └── archive/             # auto-archived fetched transcripts (purged after 14 days)
│   ├── ideas/                   # per-video markdown summaries from the LLM summary pass
│   ├── llm_comparisons/         # side-by-side N-model reports from `llm_compare`
│   ├── audio/
│   ├── fixtures/
│   ├── audit/
│   └── cache/
├── docs/
│   ├── PROJECT_DOCUMENT.md
│   └── MICRO_PHASE_PLAN.md
├── vol_crush/
│   ├── core/
│   ├── integrations/              # storage, llm (openai|openrouter), public_broker, fixtures
│   ├── idea_sources/              # YouTube/RSS/web/transcript adapters + transcript_archive
│   ├── strategy_miner/
│   ├── idea_scraper/              # summary + extraction prompts, summary_archive writer
│   ├── llm_compare/               # CLI for side-by-side multi-model comparison
│   ├── optimizer/
│   ├── executor/
│   ├── position_manager/
│   ├── position_grouping/         # deterministic leg → strategy-bundle classifier
│   ├── portfolio_sync/
│   └── backtester/
├── tests/
├── requirements.txt
└── README.md
```

## Key Configuration

**Strategy config** (two-file model — see `config/`):
- `strategy_templates.yaml`: structure-level templates (put_spread, iron_condor, short_put, ...) with entry filters, management, and regime eligibility
- `underlying_profiles.yaml`: universe groupings (index_etf, bond_etf, commodity_etf) with symbols, allowed structures, and allocation caps
- At runtime these are merged: template × eligible profile → resolved Strategy

**Portfolio constraints** in `config.yaml`:
- Beta-weighted delta: `±5%` of NLV
- Daily theta: `0.1% – 0.3%` of NLV
- Gamma/theta ratio: `< 1.5`
- Max BPR utilization: `50%`
- Max single underlying: `15%` of BPR
- Orphan leg guard: blocks new opens when unclassified short legs exist

**Regime policy defaults:**
- `high_iv`: favor premium selling
- `normal_iv`: baseline premium selling
- `low_iv`: prefer defined risk or no trade
- `event_risk`: reject new exposure

**Idea source config:**
- `idea_sources.youtube.channel_ids` — comma-separated channel IDs to poll
- `idea_sources.youtube.title_include_keywords` / `title_exclude_keywords` — optional pre-LLM filter; videos whose titles don't match are skipped before transcript fetch
- `idea_sources.rss.feed_urls`
- `idea_sources.web.urls`
- `idea_sources.transcripts.path`
- `idea_sources.transcripts_archive.path` / `retention_days` — on-disk transcript archive (default `data/transcripts/archive/`, 14 days)
- `idea_sources.summaries_archive.path` — per-video summary markdown (default `data/ideas/`)

**LLM config (`llm:` section):**
- `llm.provider` — `openai` or `openrouter`. OpenRouter auto-points the OpenAI SDK at `https://openrouter.ai/api/v1`.
- `llm.api_key`, `llm.model`, `llm.fallback_model` — when the primary errors (rate limit, 5xx, model-not-found) the client transparently retries with the fallback.
- Env overrides: `VOL_CRUSH_LLM_PROVIDER`, `VOL_CRUSH_LLM_API_KEY` (or `OPENROUTER_API_KEY`), `VOL_CRUSH_LLM_MODEL`, `VOL_CRUSH_LLM_MODEL_BACKUP`.
- Whisper audio transcription (live/record modes) still requires `provider=openai`.

## Documentation

- [Project Document](docs/PROJECT_DOCUMENT.md) — Full product and architecture spec
- [Micro-Phase Plan](docs/MICRO_PHASE_PLAN.md) — Execution status, verification, and known MVP gaps

## Status

Dry-run MVP implemented and verified. Live broker execution and Google Sheets remain
deferred. Source-adapter based idea intake is now included for transcripts, generic
web pages, RSS feeds, and YouTube channels.

## License

Private — All rights reserved.
