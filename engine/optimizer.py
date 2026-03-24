"""Parameter optimizer for FTMO strategies.

Grid search with optional random sampling, scored by FTMO-relevant metrics.
Supports walk-forward validation to detect overfitting.
"""

import itertools
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from engine.backtester import run_backtest


def _run_single(args):
    """Worker function for parallel optimization."""
    strategy_class, pairs, params, start_date, end_date = args
    try:
        result = run_backtest(
            strategy_class=strategy_class,
            pairs=pairs,
            start_date=start_date,
            end_date=end_date,
            strategy_params=params,
        )
        if "error" in result:
            return None

        ftmo = result["ftmo"]
        return {
            "params": params,
            "profit_pct": ftmo["total_profit_pct"],
            "max_dd_pct": ftmo["max_drawdown_pct"],
            "win_rate": ftmo["win_rate"],
            "trades": ftmo["total_trades"],
            "expectancy": ftmo["expectancy"],
            "avg_win": ftmo["avg_win"],
            "avg_loss": ftmo["avg_loss"],
            "profit_target_hit": ftmo["profit_target_hit"],
            "daily_loss_breached": ftmo["daily_loss_breached"],
            "total_dd_breached": ftmo["total_dd_breached"],
            "final_equity": result["final_equity"],
        }
    except Exception as e:
        return None


def score_result(r):
    """Score a backtest result. Higher = better.

    score = (expectancy × sqrt(trades)) / (1 + max_dd_pct)
    Rewards: high expectancy, many trades
    Penalizes: high drawdown
    """
    if r is None or r["trades"] < 10:
        return -999

    expectancy = r["expectancy"]
    trades = r["trades"]
    max_dd = max(r["max_dd_pct"], 0.01)

    import math
    score = (expectancy * math.sqrt(trades)) / (1 + max_dd)
    return score


def optimize_strategy(
    strategy_class,
    pairs: list[str],
    param_grid: dict,
    start_date: str = None,
    end_date: str = None,
    max_combos: int = 500,
    max_workers: int = 4,
) -> list[dict]:
    """
    Run grid search over parameter combinations.

    Args:
        strategy_class: Backtrader strategy class
        pairs: List of forex pairs
        param_grid: Dict of {param_name: [values_to_test]}
        start_date/end_date: Date range for backtest
        max_combos: Random sample if total combos exceeds this
        max_workers: Parallel processes

    Returns: List of results sorted by score (best first)
    """
    if start_date is None:
        start_date = config.BACKTEST_START
    if end_date is None:
        end_date = config.BACKTEST_END

    # Generate all parameter combinations
    keys = list(param_grid.keys())
    values = list(param_grid.values())
    all_combos = [dict(zip(keys, v)) for v in itertools.product(*values)]

    total = len(all_combos)
    print(f"Total parameter combinations: {total}")

    if total > max_combos:
        print(f"Random sampling {max_combos} of {total} combinations")
        all_combos = random.sample(all_combos, max_combos)

    print(f"Running {len(all_combos)} backtests with {max_workers} workers...")

    # Prepare args for parallel execution
    args_list = [
        (strategy_class, pairs, combo, start_date, end_date)
        for combo in all_combos
    ]

    results = []
    start_time = time.time()

    # Run in parallel
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_run_single, args): i for i, args in enumerate(args_list)}
        completed = 0
        for future in as_completed(futures):
            completed += 1
            r = future.result()
            if r is not None:
                r["score"] = score_result(r)
                results.append(r)

            if completed % 50 == 0 or completed == len(all_combos):
                elapsed = time.time() - start_time
                rate = completed / elapsed
                eta = (len(all_combos) - completed) / rate if rate > 0 else 0
                valid = len(results)
                print(f"  {completed}/{len(all_combos)} done ({valid} valid), "
                      f"ETA: {eta:.0f}s")

    # Sort by score
    results.sort(key=lambda r: r["score"], reverse=True)

    elapsed = time.time() - start_time
    print(f"\nOptimization complete: {len(results)} valid results in {elapsed:.0f}s")

    return results


def walk_forward_validate(
    strategy_class,
    pairs: list[str],
    params: dict,
    n_folds: int = 3,
) -> dict:
    """
    Anchored walk-forward validation.

    Splits 2024-2025 data into expanding train windows with OOS test periods.
    Returns in-sample and out-of-sample performance.
    """
    # Define folds (anchored: train always starts at beginning)
    folds = [
        # (train_start, train_end, test_start, test_end)
        ("2024-01-01", "2024-09-30", "2024-10-01", "2024-12-31"),
        ("2024-01-01", "2024-12-31", "2025-01-01", "2025-04-30"),
        ("2024-01-01", "2025-04-30", "2025-05-01", "2025-08-31"),
    ]

    in_sample_results = []
    out_of_sample_results = []

    for i, (ts, te, os_s, os_e) in enumerate(folds):
        # In-sample
        is_result = run_backtest(
            strategy_class=strategy_class,
            pairs=pairs,
            start_date=ts,
            end_date=te,
            strategy_params=params,
        )

        # Out-of-sample
        oos_result = run_backtest(
            strategy_class=strategy_class,
            pairs=pairs,
            start_date=os_s,
            end_date=os_e,
            strategy_params=params,
        )

        if "error" not in is_result:
            in_sample_results.append(is_result["ftmo"])
        if "error" not in oos_result:
            out_of_sample_results.append(oos_result["ftmo"])

        print(f"  Fold {i+1}: IS={is_result.get('ftmo', {}).get('total_profit_pct', 'N/A'):.1f}% | "
              f"OOS={oos_result.get('ftmo', {}).get('total_profit_pct', 'N/A'):.1f}%")

    if not in_sample_results or not out_of_sample_results:
        return {"error": "Walk-forward failed"}

    # Aggregate
    avg_is_exp = sum(r["expectancy"] for r in in_sample_results) / len(in_sample_results)
    avg_oos_exp = sum(r["expectancy"] for r in out_of_sample_results) / len(out_of_sample_results)
    avg_is_profit = sum(r["total_profit_pct"] for r in in_sample_results) / len(in_sample_results)
    avg_oos_profit = sum(r["total_profit_pct"] for r in out_of_sample_results) / len(out_of_sample_results)

    # Overfitting ratio: OOS / IS (>0.5 = acceptable)
    oof_ratio = avg_oos_exp / avg_is_exp if avg_is_exp != 0 else 0

    return {
        "avg_is_expectancy": avg_is_exp,
        "avg_oos_expectancy": avg_oos_exp,
        "avg_is_profit_pct": avg_is_profit,
        "avg_oos_profit_pct": avg_oos_profit,
        "overfitting_ratio": oof_ratio,
        "folds": n_folds,
    }


def print_top_results(results, n=10):
    """Print top N optimization results."""
    print(f"\n{'='*90}")
    print(f"TOP {min(n, len(results))} PARAMETER COMBINATIONS")
    print(f"{'='*90}")

    for i, r in enumerate(results[:n]):
        print(f"\n#{i+1} (score: {r['score']:.1f})")
        print(f"  Params: {r['params']}")
        print(f"  Profit: {r['profit_pct']:+.1f}% | Max DD: {r['max_dd_pct']:.1f}% | "
              f"WR: {r['win_rate']:.1f}% | Trades: {r['trades']} | "
              f"Expectancy: ${r['expectancy']:.2f}")
        actual_rr = abs(r['avg_win'] / r['avg_loss']) if r['avg_loss'] != 0 else 0
        print(f"  Avg Win: ${r['avg_win']:.0f} | Avg Loss: ${r['avg_loss']:.0f} | "
              f"Actual R:R: {actual_rr:.2f}:1")
