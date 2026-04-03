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
Module 1: Idea Intake ─────── source adapters → raw docs → LLM extraction → local store
Fixtures ──────────────────── local bundle built from sibling repo data + public seeds
Module 2: Portfolio Optimizer ─ deterministic Greek-aware combo scoring
Module 3: Pending Executor ── size and emit dry-run/pending orders
Module 4: Position Manager ── recommend closes/rolls/adjustments
Backtester ────────────────── replay gate from imported fixture trades
```

## Quick Start

```bash
cd kamandal
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

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
```

## Project Structure

```text
kamandal/
├── config/
│   ├── config.example.yaml
│   └── strategies.yaml
├── data/
│   ├── transcripts/
│   ├── audio/
│   ├── fixtures/
│   ├── audit/
│   └── cache/
├── docs/
│   ├── PROJECT_DOCUMENT.md
│   └── MICRO_PHASE_PLAN.md
├── vol_crush/
│   ├── core/
│   ├── integrations/
│   ├── idea_sources/
│   ├── strategy_miner/
│   ├── idea_scraper/
│   ├── optimizer/
│   ├── executor/
│   ├── position_manager/
│   └── backtester/
├── tests/
├── requirements.txt
└── README.md
```

## Key Configuration

Portfolio constraints in `config.yaml`:

- Beta-weighted delta: `±5%` of NLV
- Daily theta: `0.1% – 0.3%` of NLV
- Gamma/theta ratio: `< 1.5`
- Max BPR utilization: `50%`
- Max single underlying: `15%` of BPR

Regime policy defaults:

- `high_iv`: favor premium selling
- `normal_iv`: baseline premium selling
- `low_iv`: prefer defined risk or no trade
- `event_risk`: reject new exposure

Idea source config:

- `idea_sources.youtube.channel_ids`
- `idea_sources.rss.feed_urls`
- `idea_sources.web.urls`
- `idea_sources.transcripts.path`

## Documentation

- [Project Document](docs/PROJECT_DOCUMENT.md) — Full product and architecture spec
- [Micro-Phase Plan](docs/MICRO_PHASE_PLAN.md) — Execution status, verification, and known MVP gaps

## Status

Dry-run MVP implemented and verified. Live broker execution and Google Sheets remain
deferred. Source-adapter based idea intake is now included for transcripts, generic
web pages, RSS feeds, and YouTube channels.

## License

Private — All rights reserved.
