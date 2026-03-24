"""CLI entry point for running backtests and optimization."""

import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config
from engine.backtester import run_backtest, simulate_ftmo_challenges
from engine.reporter import print_backtest_report, print_simulation_report, console
from strategies.london_breakout import LondonBreakout
from strategies.bb_squeeze import BBSqueeze
from strategies.fvg import FVG
from strategies.ensemble import Ensemble

STRATEGIES = {
    "london": {
        "class": LondonBreakout,
        "pairs": config.LONDON_BREAKOUT_PAIRS,
        "name": "London Session Breakout",
    },
    "bb_squeeze": {
        "class": BBSqueeze,
        "pairs": config.BB_SQUEEZE_PAIRS,
        "name": "Bollinger Squeeze Breakout",
    },
    "fvg": {
        "class": FVG,
        "pairs": config.FVG_PAIRS,
        "name": "ICT Fair Value Gap",
    },
    "ensemble": {
        "class": Ensemble,
        "pairs": config.ENSEMBLE_PAIRS,
        "name": "Ensemble (ER + LB + Squeeze)",
    },
}

# Parameter grids for optimization
PARAM_GRIDS = {
    "london": {
        "risk_reward": [1.5, 2.0, 2.5, 3.0],
        "min_range_pips": [15, 20, 30, 40],
        "max_range_pips": [60, 80, 100],
        "max_sl_pips": [30, 40, 50],
        "london_entry_end": [9, 10, 11],
        "risk_pct": [0.01, 0.015, 0.02, 0.03],
        "day_filter": [[1, 2, 3], [0, 1, 2, 3], [0, 1, 2, 3, 4]],
    },
    "bb_squeeze": {
        "squeeze_min_bars": [3, 4, 6, 8],
        "release_window": [3, 6, 10],
        "risk_reward": [1.5, 2.0, 2.5, 3.0],
        "max_sl_pips": [25, 35, 50],
        "risk_pct": [0.01, 0.015, 0.02, 0.03],
        "min_body_ratio": [0.3, 0.5, 0.7],
        "kc_atr_mult": [1.0, 1.5, 2.0],
    },
    "fvg": {
        "risk_reward": [1.5, 2.0, 2.5],
        "min_impulse_atr_mult": [1.0, 1.3, 1.5, 2.0],
        "max_fvg_age": [24, 48, 96],
        "max_sl_pips": [25, 35, 50],
        "risk_pct": [0.01, 0.015, 0.02, 0.03],
        "ema_period": [30, 50, 100],
        "min_gap_atr_ratio": [0.2, 0.3, 0.5],
    },
    "ensemble": {
        # Shared risk — most impactful param
        "risk_pct": [0.01, 0.015, 0.02, 0.025, 0.03],
        # ER: z_threshold controls trade frequency vs quality
        "er_z_threshold": [1.5, 2.0, 2.5],
        "er_risk_reward": [1.5, 2.0, 2.5],
        # LB: R:R and range filter are the biggest levers
        "lb_risk_reward": [1.5, 2.0, 2.5, 3.0],
        "lb_min_range_pips": [15, 20, 30],
        # SQ: squeeze duration and R:R
        "sq_squeeze_min_bars": [3, 4, 6],
        "sq_risk_reward": [1.5, 2.0, 2.5],
    },  # 5*3*3*4*3*3*3 = 4860 combos
}


def run_single(strategy_key: str):
    strat = STRATEGIES[strategy_key]
    console.print(f"\n[bold]Running {strat['name']}...[/bold]\n")

    result = run_backtest(
        strategy_class=strat["class"],
        pairs=strat["pairs"],
    )

    if "error" in result:
        console.print(f"[red]Error: {result['error']}[/red]")
        return

    print_backtest_report(strat["name"], strat["pairs"], result)
    return result


def run_simulation(strategy_key: str, num_sims: int = 50):
    strat = STRATEGIES[strategy_key]
    console.print(f"\n[bold]Simulating FTMO challenges for {strat['name']}...[/bold]\n")

    sim_result = simulate_ftmo_challenges(
        strategy_class=strat["class"],
        pairs=strat["pairs"],
        num_simulations=num_sims,
    )

    if "error" in sim_result:
        console.print(f"[red]Error: {sim_result['error']}[/red]")
        return

    print_simulation_report(strat["name"], sim_result)
    return sim_result


def run_optimize(strategy_key: str, max_combos: int = 200, max_workers: int = 4):
    from engine.optimizer import optimize_strategy, walk_forward_validate, print_top_results

    strat = STRATEGIES[strategy_key]
    grid = PARAM_GRIDS.get(strategy_key)

    if not grid:
        console.print(f"[red]No parameter grid defined for '{strategy_key}'[/red]")
        return

    console.print(f"\n[bold]Optimizing {strat['name']}...[/bold]\n")

    results = optimize_strategy(
        strategy_class=strat["class"],
        pairs=strat["pairs"],
        param_grid=grid,
        max_combos=max_combos,
        max_workers=max_workers,
    )

    if not results:
        console.print("[red]No valid results from optimization[/red]")
        return

    print_top_results(results, n=15)

    # Walk-forward validate top 3
    console.print(f"\n[bold]Walk-forward validating top 3...[/bold]")
    for i, r in enumerate(results[:3]):
        console.print(f"\n[cyan]#{i+1} params: {r['params']}[/cyan]")
        wf = walk_forward_validate(
            strategy_class=strat["class"],
            pairs=strat["pairs"],
            params=r["params"],
        )
        if "error" not in wf:
            oof = wf["overfitting_ratio"]
            status = "[green]PASS[/green]" if oof > 0.5 else "[red]OVERFIT[/red]"
            console.print(f"  IS expectancy: ${wf['avg_is_expectancy']:.2f} | "
                          f"OOS expectancy: ${wf['avg_oos_expectancy']:.2f} | "
                          f"Ratio: {oof:.2f} {status}")
            console.print(f"  IS profit: {wf['avg_is_profit_pct']:+.1f}% | "
                          f"OOS profit: {wf['avg_oos_profit_pct']:+.1f}%")

    return results


def run_all():
    console.print("\n[bold]=" * 60)
    console.print("[bold]FTMO STRATEGY BACKTESTER — ALL STRATEGIES[/bold]")
    console.print("[bold]=" * 60)

    results = {}
    for key in STRATEGIES:
        result = run_single(key)
        if result:
            results[key] = result

    if results:
        from rich.table import Table
        table = Table(title="Strategy Comparison", show_lines=True)
        table.add_column("Strategy", style="cyan")
        table.add_column("FTMO", justify="center")
        table.add_column("Profit", justify="right")
        table.add_column("Max DD", justify="right")
        table.add_column("Win Rate", justify="right")
        table.add_column("Trades", justify="right")
        table.add_column("Expectancy", justify="right")

        for key, result in results.items():
            ftmo = result["ftmo"]
            name = STRATEGIES[key]["name"]
            status = "[green]PASS[/green]" if ftmo["passed"] else "[red]FAIL[/red]"
            table.add_row(
                name,
                status,
                f"{ftmo['total_profit_pct']:+.2f}%",
                f"{ftmo['max_drawdown_pct']:.2f}%",
                f"{ftmo['win_rate']:.1f}%",
                str(ftmo["total_trades"]),
                f"${ftmo['expectancy']:,.2f}",
            )

        console.print(table)


def main():
    usage = """
Usage: python run_backtest.py <command> [strategy] [options]

Commands:
  single <strategy>              Run full backtest for one strategy
  simulate <strategy> [n]        Simulate n FTMO challenges (default 50)
  optimize <strategy> [n]        Optimize params (n = max combos, default 300)
  all                            Run all strategies and compare

Strategies: london, bb_squeeze, fvg, ensemble
    """

    if len(sys.argv) < 2:
        print(usage)
        return

    command = sys.argv[1]

    if command == "all":
        run_all()
    elif command in ("single", "simulate", "optimize"):
        if len(sys.argv) < 3:
            print(f"Please specify a strategy: {', '.join(STRATEGIES.keys())}")
            return
        strategy_key = sys.argv[2]
        if strategy_key not in STRATEGIES:
            print(f"Unknown strategy '{strategy_key}'. Options: {', '.join(STRATEGIES.keys())}")
            return
        if command == "single":
            run_single(strategy_key)
        elif command == "simulate":
            num_sims = int(sys.argv[3]) if len(sys.argv) > 3 else 50
            run_simulation(strategy_key, num_sims)
        else:
            max_combos = int(sys.argv[3]) if len(sys.argv) > 3 else 300
            max_workers = int(sys.argv[4]) if len(sys.argv) > 4 else 4
            run_optimize(strategy_key, max_combos, max_workers)
    else:
        print(usage)


if __name__ == "__main__":
    main()
