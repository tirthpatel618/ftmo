"""Generate formatted backtest reports."""

from rich.console import Console
from rich.table import Table
from rich.panel import Panel


console = Console()


def print_backtest_report(strategy_name: str, pairs: list[str], result: dict):
    """Print a formatted backtest report."""
    ftmo = result["ftmo"]
    trades = result["trades"]

    # Header
    status = "[bold green]PASS[/bold green]" if ftmo["passed"] else "[bold red]FAIL[/bold red]"
    console.print(Panel(
        f"Strategy: [bold]{strategy_name}[/bold]  |  "
        f"Pairs: {', '.join(pairs)}  |  "
        f"FTMO: {status}",
        title="Backtest Report",
    ))

    # FTMO Rules
    table = Table(title="FTMO Challenge Rules", show_lines=True)
    table.add_column("Rule", style="cyan")
    table.add_column("Status", justify="center")
    table.add_column("Value", justify="right")

    target_status = "[green]HIT[/green]" if ftmo["profit_target_hit"] else "[red]MISS[/red]"
    table.add_row("Profit Target (10%)", target_status, f"{ftmo['total_profit_pct']:+.2f}%")

    daily_status = "[red]BREACHED[/red]" if ftmo["daily_loss_breached"] else "[green]OK[/green]"
    table.add_row("Daily Loss < 5%", daily_status, f"Max DD: {ftmo['max_drawdown_pct']:.2f}%")

    dd_status = "[red]BREACHED[/red]" if ftmo["total_dd_breached"] else "[green]OK[/green]"
    table.add_row("Total DD < 10%", dd_status, f"{ftmo['max_drawdown_pct']:.2f}%")

    days_status = "[green]OK[/green]" if ftmo["min_days_met"] else "[red]FAIL[/red]"
    table.add_row("Min Trading Days (4)", days_status, f"{ftmo['trading_days']} days")

    consistency_status = "[green]OK[/green]" if ftmo["consistency_ok"] else "[red]FAIL[/red]"
    table.add_row("Consistency", consistency_status, "")

    console.print(table)

    # Trade Stats
    if trades.get("total", 0) > 0:
        stats = Table(title="Trade Statistics", show_lines=True)
        stats.add_column("Metric", style="cyan")
        stats.add_column("Value", justify="right")

        stats.add_row("Total Trades", str(trades["total"]))
        stats.add_row("Win Rate", f"{trades['win_rate']:.1f}%")
        stats.add_row("Wins / Losses", f"{trades['wins']} / {trades['losses']}")
        stats.add_row("Avg Win", f"${trades['avg_win']:,.2f}")
        stats.add_row("Avg Loss", f"${trades['avg_loss']:,.2f}")
        stats.add_row("Best Trade", f"${trades['best_trade']:,.2f}")
        stats.add_row("Worst Trade", f"${trades['worst_trade']:,.2f}")
        stats.add_row("Total P/L", f"${trades['total_pnl']:,.2f}")
        stats.add_row("Avg Bars Held", f"{trades['avg_bars_held']:.1f}")
        if result.get("sharpe"):
            stats.add_row("Sharpe Ratio", f"{result['sharpe']:.2f}")

        console.print(stats)

    # Final result
    console.print(f"\n  Final Equity: [bold]${ftmo['final_equity']:,.2f}[/bold]")
    console.print(f"  Expectancy: [bold]${ftmo['expectancy']:,.2f}[/bold] per trade")
    console.print()


def print_simulation_report(strategy_name: str, sim_result: dict):
    """Print FTMO challenge simulation results."""
    console.print(Panel(
        f"Strategy: [bold]{strategy_name}[/bold]  |  "
        f"Simulations: {sim_result['total_simulations']}  |  "
        f"Pass Rate: [bold {'green' if sim_result['pass_rate'] > 15 else 'red'}]"
        f"{sim_result['pass_rate']:.1f}%[/bold]",
        title="FTMO Challenge Simulation",
    ))

    table = Table(show_lines=True)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")

    table.add_row("Total Simulations", str(sim_result["total_simulations"]))
    table.add_row("Challenges Passed", f"{sim_result['passed']} ({sim_result['pass_rate']:.1f}%)")
    table.add_row("Profit Target Hit", str(sim_result["target_hits"]))
    table.add_row("Daily Loss Breaches", str(sim_result["daily_breaches"]))
    table.add_row("Max DD Breaches", str(sim_result["dd_breaches"]))
    table.add_row("Avg Profit", f"{sim_result['avg_profit_pct']:+.2f}%")
    table.add_row("Avg Max Drawdown", f"{sim_result['avg_max_dd_pct']:.2f}%")
    table.add_row("Avg Win Rate", f"{sim_result['avg_win_rate']:.1f}%")
    table.add_row("Avg Trades per Challenge", f"{sim_result['avg_trades']:.0f}")

    console.print(table)

    # Verdict
    rate = sim_result["pass_rate"]
    if rate >= 20:
        console.print("\n  [bold green]STRONG — Strategy is worth forward-testing on MT5 demo[/bold green]")
    elif rate >= 10:
        console.print("\n  [bold yellow]MODERATE — Needs optimization but has potential[/bold yellow]")
    else:
        console.print("\n  [bold red]WEAK — Strategy unlikely to pass FTMO consistently[/bold red]")

    console.print()
