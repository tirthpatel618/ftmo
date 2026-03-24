"""Backtrader-based backtesting engine with FTMO rules."""

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import backtrader as bt
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from engine.ftmo_rules import FTMOAnalyzer, TradeStats


def load_data(pair: str, timeframe_minutes: int = None) -> bt.feeds.PandasData:
    """Load CSV data into a Backtrader data feed."""
    if timeframe_minutes is None:
        timeframe_minutes = config.TIMEFRAME_MINUTES

    data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), config.DATA_DIR)
    csv_path = os.path.join(data_dir, f"{pair}_{timeframe_minutes}m.csv")

    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Data file not found: {csv_path}. Run data/download.py first.")

    df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
    df.columns = [c.lower() for c in df.columns]

    # Ensure required columns exist
    for col in ["open", "high", "low", "close"]:
        if col not in df.columns:
            raise ValueError(f"Missing column '{col}' in {csv_path}")

    if "volume" not in df.columns:
        df["volume"] = 0

    data = bt.feeds.PandasData(
        dataname=df,
        datetime=None,  # index is datetime
        open="open",
        high="high",
        low="low",
        close="close",
        volume="volume",
        openinterest=-1,
        timeframe=bt.TimeFrame.Minutes,
        compression=timeframe_minutes,
    )
    return data


def run_backtest(
    strategy_class,
    pairs: list[str],
    start_date: str = None,
    end_date: str = None,
    initial_balance: float = None,
    profit_target_pct: float = None,
    strategy_params: dict = None,
) -> dict:
    """
    Run a backtest for a strategy across one or more pairs.

    Returns dict with FTMO analysis, trade stats, and final equity.
    """
    if initial_balance is None:
        initial_balance = config.INITIAL_BALANCE
    if profit_target_pct is None:
        profit_target_pct = config.PROFIT_TARGET_PCT

    cerebro = bt.Cerebro()

    # Add strategy with optional params
    if strategy_params:
        cerebro.addstrategy(strategy_class, **strategy_params)
    else:
        cerebro.addstrategy(strategy_class)

    # Load data for each pair
    for pair in pairs:
        try:
            data = load_data(pair)
            data._name = pair
            cerebro.adddata(data, name=pair)
        except FileNotFoundError as e:
            print(f"Warning: {e}")
            continue

    if not cerebro.datas:
        return {"error": "No data loaded"}

    # Broker settings
    cerebro.broker.setcash(initial_balance)
    # Forex broker: 1:100 leverage (FTMO standard), spread as commission
    cerebro.broker.setcommission(
        commission=config.COMMISSION_PIPS * 0.0001,  # 2 pips spread cost
        commtype=bt.CommInfoBase.COMM_FIXED,
        stocklike=False,
        leverage=100.0,  # FTMO offers 1:100 forex leverage
    )

    # Analyzers
    cerebro.addanalyzer(
        FTMOAnalyzer,
        _name="ftmo",
        initial_balance=initial_balance,
        profit_target_pct=profit_target_pct,
    )
    cerebro.addanalyzer(TradeStats, _name="trades")
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", riskfreerate=0.0)
    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")

    # Run
    results = cerebro.run()
    strat = results[0]

    ftmo_analysis = strat.analyzers.ftmo.get_analysis()
    trade_analysis = strat.analyzers.trades.get_analysis()
    sharpe = strat.analyzers.sharpe.get_analysis()

    return {
        "ftmo": ftmo_analysis,
        "trades": trade_analysis,
        "sharpe": sharpe.get("sharperatio", 0),
        "final_equity": cerebro.broker.getvalue(),
    }


def simulate_ftmo_challenges(
    strategy_class,
    pairs: list[str],
    challenge_days: int = None,
    num_simulations: int = 50,
    strategy_params: dict = None,
) -> dict:
    """
    Simulate multiple rolling FTMO challenges across the historical data.

    Slides a 30-day window across the data and checks if the strategy
    would have passed each window.
    """
    if challenge_days is None:
        challenge_days = config.CHALLENGE_DAYS

    # Load full dataset to get date range
    data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), config.DATA_DIR)
    first_pair = pairs[0]
    csv_path = os.path.join(data_dir, f"{first_pair}_{config.TIMEFRAME_MINUTES}m.csv")
    df = pd.read_csv(csv_path, index_col=0, parse_dates=True)

    start = df.index[0].to_pydatetime()
    end = df.index[-1].to_pydatetime()
    total_range = (end - start).days

    if total_range < challenge_days:
        return {"error": "Not enough data for simulation"}

    # Generate evenly spaced start dates
    step = max(1, (total_range - challenge_days) // num_simulations)
    results = []

    print(f"\nSimulating {num_simulations} FTMO challenges ({challenge_days} days each)...")

    for i in range(num_simulations):
        window_start = start + timedelta(days=i * step)
        window_end = window_start + timedelta(days=challenge_days)

        if window_end > end:
            break

        # Filter data to window and run backtest
        result = run_backtest(
            strategy_class=strategy_class,
            pairs=pairs,
            start_date=window_start.strftime("%Y-%m-%d"),
            end_date=window_end.strftime("%Y-%m-%d"),
            strategy_params=strategy_params,
        )

        if "error" in result:
            continue

        ftmo = result["ftmo"]
        results.append({
            "window_start": window_start.strftime("%Y-%m-%d"),
            "passed": ftmo["passed"],
            "profit_pct": ftmo["total_profit_pct"],
            "max_dd_pct": ftmo["max_drawdown_pct"],
            "win_rate": ftmo["win_rate"],
            "trades": ftmo["total_trades"],
            "daily_breach": ftmo["daily_loss_breached"],
            "dd_breach": ftmo["total_dd_breached"],
            "target_hit": ftmo["profit_target_hit"],
        })

        status = "PASS" if ftmo["passed"] else "FAIL"
        reason = ""
        if not ftmo["passed"]:
            if ftmo["daily_loss_breached"]:
                reason = "(daily loss)"
            elif ftmo["total_dd_breached"]:
                reason = "(max DD)"
            elif not ftmo["profit_target_hit"]:
                reason = "(no target)"
            elif not ftmo["min_days_met"]:
                reason = "(min days)"
        print(f"  [{i+1}/{num_simulations}] {window_start:%Y-%m-%d}: {status} {reason} "
              f"P/L: {ftmo['total_profit_pct']:+.1f}% | DD: {ftmo['max_drawdown_pct']:.1f}% | "
              f"Trades: {ftmo['total_trades']} | WR: {ftmo['win_rate']:.0f}%")

    if not results:
        return {"error": "No simulations completed"}

    passed = sum(1 for r in results if r["passed"])
    total = len(results)

    return {
        "total_simulations": total,
        "passed": passed,
        "pass_rate": passed / total * 100,
        "avg_profit_pct": sum(r["profit_pct"] for r in results) / total,
        "avg_max_dd_pct": sum(r["max_dd_pct"] for r in results) / total,
        "avg_win_rate": sum(r["win_rate"] for r in results) / total,
        "avg_trades": sum(r["trades"] for r in results) / total,
        "daily_breaches": sum(1 for r in results if r["daily_breach"]),
        "dd_breaches": sum(1 for r in results if r["dd_breach"]),
        "target_hits": sum(1 for r in results if r["target_hit"]),
        "results": results,
    }
