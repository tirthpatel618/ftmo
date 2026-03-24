"""FTMO Challenge constraint tracking as a Backtrader Analyzer."""

import backtrader as bt
from collections import defaultdict


class FTMOAnalyzer(bt.Analyzer):
    """
    Tracks all FTMO challenge constraints during a backtest.

    Reports:
    - Whether daily loss limit was breached
    - Whether max drawdown was breached
    - Whether profit target was hit
    - Number of trading days
    - Consistency (max single-day profit as % of total)
    - Full equity curve for analysis
    """

    params = (
        ("initial_balance", 100_000),
        ("max_daily_loss_pct", 0.05),
        ("max_total_dd_pct", 0.10),
        ("profit_target_pct", 0.10),
        ("min_trading_days", 4),
        ("max_single_day_profit_pct", 0.40),  # consistency: no day > 40% of total profit
    )

    def start(self):
        self.equity_curve = []
        self.daily_equity = {}  # date -> (start_equity, min_equity, end_equity)
        self.trading_days = set()
        self.daily_pnl = defaultdict(float)
        self.trade_log = []

        self._current_date = None
        self._day_start_equity = self.p.initial_balance
        self._peak_equity = self.p.initial_balance
        self._breached_daily = False
        self._breached_total = False
        self._breach_date = None
        self._target_hit = False
        self._target_date = None

    def next(self):
        dt = self.strategy.datetime.datetime(0)
        today = dt.date()
        equity = self.strategy.broker.getvalue()

        # Record equity curve
        self.equity_curve.append({"datetime": dt, "equity": equity})

        # New day detection
        if today != self._current_date:
            if self._current_date is not None:
                # Save previous day's data
                self.daily_equity[self._current_date] = {
                    "start": self._day_start_equity,
                    "end": equity,
                }
            self._day_start_equity = max(equity, self.strategy.broker.getvalue())
            self._current_date = today

        # Track if positions were opened/held today
        if self.strategy.position.size != 0:
            self.trading_days.add(today)

        # Daily loss check: equity vs start-of-day equity (or balance, whichever higher)
        daily_loss = self._day_start_equity - equity
        max_daily_loss = self.p.initial_balance * self.p.max_daily_loss_pct
        if daily_loss >= max_daily_loss and not self._breached_daily:
            self._breached_daily = True
            self._breach_date = today

        # Total drawdown check
        self._peak_equity = max(self._peak_equity, equity)
        max_dd_amount = self.p.initial_balance * self.p.max_total_dd_pct
        floor = self.p.initial_balance - max_dd_amount
        if equity <= floor and not self._breached_total:
            self._breached_total = True
            self._breach_date = today

        # Profit target check
        profit = equity - self.p.initial_balance
        target = self.p.initial_balance * self.p.profit_target_pct
        if profit >= target and not self._target_hit:
            self._target_hit = True
            self._target_date = today

    def notify_trade(self, trade):
        """Track closed trades for daily PnL."""
        if trade.isclosed:
            close_date = bt.num2date(trade.dtclose).date()
            self.daily_pnl[close_date] += trade.pnl
            self.trading_days.add(close_date)
            self.trade_log.append({
                "open_date": bt.num2date(trade.dtopen),
                "close_date": bt.num2date(trade.dtclose),
                "pnl": trade.pnl,
                "size": trade.size,
                "bar_len": trade.barlen,
            })

    def get_analysis(self):
        equity = self.strategy.broker.getvalue()
        total_profit = equity - self.p.initial_balance
        total_trades = len(self.trade_log)
        winning_trades = sum(1 for t in self.trade_log if t["pnl"] > 0)
        losing_trades = sum(1 for t in self.trade_log if t["pnl"] <= 0)

        # Consistency check
        max_day_profit = max(self.daily_pnl.values()) if self.daily_pnl else 0
        consistency_ok = True
        if total_profit > 0 and max_day_profit > 0:
            max_day_pct = max_day_profit / total_profit
            consistency_ok = max_day_pct <= self.p.max_single_day_profit_pct

        # Max drawdown from equity curve
        peak = self.p.initial_balance
        max_dd = 0
        for point in self.equity_curve:
            peak = max(peak, point["equity"])
            dd = peak - point["equity"]
            max_dd = max(max_dd, dd)

        # Win rate and expectancy
        win_rate = winning_trades / total_trades if total_trades > 0 else 0
        avg_win = (
            sum(t["pnl"] for t in self.trade_log if t["pnl"] > 0) / winning_trades
            if winning_trades > 0
            else 0
        )
        avg_loss = (
            abs(sum(t["pnl"] for t in self.trade_log if t["pnl"] <= 0)) / losing_trades
            if losing_trades > 0
            else 0
        )
        expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

        passed = (
            self._target_hit
            and not self._breached_daily
            and not self._breached_total
            and len(self.trading_days) >= self.p.min_trading_days
            and consistency_ok
        )

        return {
            "passed": passed,
            "profit_target_hit": self._target_hit,
            "target_date": self._target_date,
            "daily_loss_breached": self._breached_daily,
            "total_dd_breached": self._breached_total,
            "breach_date": self._breach_date,
            "trading_days": len(self.trading_days),
            "min_days_met": len(self.trading_days) >= self.p.min_trading_days,
            "consistency_ok": consistency_ok,
            "total_profit": total_profit,
            "total_profit_pct": total_profit / self.p.initial_balance * 100,
            "max_drawdown": max_dd,
            "max_drawdown_pct": max_dd / self.p.initial_balance * 100,
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "win_rate": win_rate * 100,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "expectancy": expectancy,
            "final_equity": equity,
        }


class TradeStats(bt.Analyzer):
    """Simple trade statistics tracker."""

    def start(self):
        self.trades = []

    def notify_trade(self, trade):
        if trade.isclosed:
            self.trades.append({
                "pnl": trade.pnl,
                "pnl_pct": trade.pnl / trade.price * 100 if trade.price else 0,
                "size": trade.size,
                "bars": trade.barlen,
                "commission": trade.commission,
            })

    def get_analysis(self):
        if not self.trades:
            return {"total": 0}
        pnls = [t["pnl"] for t in self.trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        return {
            "total": len(pnls),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(pnls) * 100,
            "total_pnl": sum(pnls),
            "avg_win": sum(wins) / len(wins) if wins else 0,
            "avg_loss": sum(losses) / len(losses) if losses else 0,
            "best_trade": max(pnls),
            "worst_trade": min(pnls),
            "avg_bars_held": sum(t["bars"] for t in self.trades) / len(self.trades),
        }
