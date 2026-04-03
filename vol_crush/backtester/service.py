"""Basic replay/backtest gate for Vol Crush strategies."""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

from vol_crush.core.config import load_config, load_strategies, save_strategies
from vol_crush.core.logging import setup_logging
from vol_crush.core.models import BacktestResult, ReplayTrade, Strategy
from vol_crush.integrations.fixtures import load_replay_trades
from vol_crush.integrations.storage import build_local_store

logger = logging.getLogger("vol_crush.backtester")


def _max_drawdown(pnls: list[float]) -> float:
    running = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        running += pnl
        peak = max(peak, running)
        max_drawdown = min(max_drawdown, running - peak)
    return abs(max_drawdown)


def _filter_trades(strategy: Strategy, trades: list[ReplayTrade]) -> list[ReplayTrade]:
    if strategy.filters.underlyings:
        return [trade for trade in trades if trade.underlying in strategy.filters.underlyings]
    return trades


def evaluate_strategy(strategy: Strategy, trades: list[ReplayTrade], config: dict) -> BacktestResult:
    relevant = _filter_trades(strategy, trades)
    total = len(relevant)
    wins = sum(1 for trade in relevant if trade.is_winner)
    losses = total - wins
    total_pnl = sum(trade.profit_pct for trade in relevant)
    avg_pnl = total_pnl / total if total else 0.0
    theta_proxy = sum(trade.theta_capture_proxy for trade in relevant) / total if total else 0.0
    avg_days = sum(trade.days_in_trade for trade in relevant) / total if total else 0.0
    win_rate = wins / total if total else 0.0
    max_drawdown = _max_drawdown([trade.profit_pct for trade in relevant])
    thresholds = config.get("backtesting", {}).get("approval_thresholds", {})
    approved = bool(
        total
        and win_rate >= thresholds.get("min_win_rate", 0.65)
        and max_drawdown <= thresholds.get("max_drawdown_pct", 25.0)
    )
    return BacktestResult(
        strategy_id=strategy.id,
        test_date=datetime.now(timezone.utc).date().isoformat(),
        period_start=relevant[0].symbol if relevant else "",
        period_end=relevant[-1].symbol if relevant else "",
        total_trades=total,
        winning_trades=wins,
        losing_trades=losses,
        win_rate=round(win_rate, 4),
        avg_pnl_per_trade=round(avg_pnl, 4),
        total_pnl=round(total_pnl, 4),
        max_drawdown_pct=round(max_drawdown, 4),
        sharpe_ratio=round((avg_pnl / max_drawdown) if max_drawdown else avg_pnl, 4),
        theta_efficiency=round(theta_proxy, 4),
        approved=approved,
    )


def run_backtests(config: dict) -> list[BacktestResult]:
    store = build_local_store(config)
    raw_strategies = load_strategies()
    strategies = [Strategy.from_dict(item) for item in raw_strategies]
    trades = store.list_replay_trades() or load_replay_trades(config)
    results = [evaluate_strategy(strategy, trades, config) for strategy in strategies]
    for result in results:
        store.save_backtest_result(result)
    if raw_strategies:
        for item in raw_strategies:
            matching = next((result for result in results if result.strategy_id == item.get("id")), None)
            if matching:
                item["backtest_approved"] = matching.approved
        save_strategies(raw_strategies)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Vol Crush replay backtester")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    logger = setup_logging(config.get("app", {}).get("log_level", "INFO"))
    results = run_backtests(config)
    logger.info("Completed %d backtest evaluations", len(results))


if __name__ == "__main__":
    main()
