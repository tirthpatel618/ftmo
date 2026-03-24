"""Base mixin for FTMO-compliant strategies.

Adds circuit breakers that stop trading before FTMO limits are breached:
- Stop trading for the day if daily loss reaches 4% (buffer before 5% limit)
- Stop trading entirely if total drawdown reaches 8% (buffer before 10% limit)
"""

import backtrader as bt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config


class FTMOBase(bt.Strategy):
    """Base class with FTMO risk circuit breakers."""

    params = (
        ("daily_loss_limit", 0.04),   # stop trading at 4% daily loss (FTMO limit is 5%)
        ("total_dd_limit", 0.08),     # stop trading at 8% total DD (FTMO limit is 10%)
        ("use_circuit_breaker", False),  # only enable for FTMO simulations
    )

    def _init_ftmo(self):
        """Call this at the end of subclass __init__."""
        self._initial_balance = config.INITIAL_BALANCE
        self._day_start_equity = self._initial_balance
        self._peak_equity = self._initial_balance
        self._current_day = None
        self._daily_halted = False
        self._total_halted = False

    def _check_ftmo_limits(self):
        """Check circuit breakers. Returns True if trading is allowed."""
        if not self.p.use_circuit_breaker:
            return True

        equity = self.broker.getvalue()
        dt = self.datas[0].datetime.datetime(0)
        today = dt.date()

        # New day — reset daily tracking
        if today != self._current_day:
            self._current_day = today
            self._day_start_equity = equity
            self._daily_halted = False

        # Update peak
        if equity > self._peak_equity:
            self._peak_equity = equity

        # Total drawdown check (from initial balance, not peak — FTMO rule)
        total_dd = (self._initial_balance - equity) / self._initial_balance
        if total_dd >= self.p.total_dd_limit:
            if not self._total_halted:
                self._total_halted = True
            return False

        # Daily loss check
        daily_loss = (self._day_start_equity - equity) / self._day_start_equity
        if daily_loss >= self.p.daily_loss_limit:
            if not self._daily_halted:
                self._daily_halted = True
            return False

        return True

    def _close_all_positions(self):
        """Emergency close all open positions."""
        for d in self.datas:
            if self.getposition(d).size != 0:
                self.close(data=d)
