"""CLI entry point for running backtests."""

import sys
import os
from pathlib import Path

# Ensure project root is in path
sys.path.insert(0, str(Path(__file__).parent))

import config
from engine.backtester import run_backtest, simulate_ftmo_challenges
from engine.reporter import print_backtest_report, print_simulation_report, console
from strategies.london_breakout import LondonBreakout
from strategies.mean_reversion import MeanReversion
from strategies.ny_momentum import NYMomentum

STRATEGIES = {
    "london": {
        "class": LondonBreakout,
        "pairs": config.LONDON_BREAKOUT_PAIRS,
        "name": "London Session Breakout",
    },
    "mean_reversion": {
        "class": MeanReversion,
        "pairs": config.MEAN_REVERSION_PAIRS,
        "name": "Mean Reversion (Ranging Pairs)",
    },
    "ny_momentum": {
        "class": NYMomentum,
        "pairs": config.NY_MOMENTUM_PAIRS,
        "name": "NY Session Momentum",
    },
}


def run_single(strategy_key: str):
    """Run a single strategy backtest on full data."""
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
    """Simulate multiple FTMO challenges for a strategy."""
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


def run_all():
    """Run all strategies and compare."""
    console.print("\n[bold]=" * 60)
    console.print("[bold]FTMO STRATEGY BACKTESTER — ALL STRATEGIES[/bold]")
    console.print("[bold]=" * 60)

    results = {}
    for key in STRATEGIES:
        result = run_single(key)
        if result:
            results[key] = result

    # Comparison table
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
Usage: python run_backtest.py <command> [strategy]

Commands:
  single <strategy>     Run full backtest for one strategy
  simulate <strategy>   Simulate 50 FTMO challenges
  all                   Run all strategies and compare

Strategies: london, mean_reversion, ny_momentum
    """

    if len(sys.argv) < 2:
        print(usage)
        return

    command = sys.argv[1]

    if command == "all":
        run_all()
    elif command in ("single", "simulate"):
        if len(sys.argv) < 3:
            print(f"Please specify a strategy: {', '.join(STRATEGIES.keys())}")
            return
        strategy_key = sys.argv[2]
        if strategy_key not in STRATEGIES:
            print(f"Unknown strategy '{strategy_key}'. Options: {', '.join(STRATEGIES.keys())}")
            return
        if command == "single":
            run_single(strategy_key)
        else:
            num_sims = int(sys.argv[3]) if len(sys.argv) > 3 else 50
            run_simulation(strategy_key, num_sims)
    else:
        print(usage)


if __name__ == "__main__":
    main()
