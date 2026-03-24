"""Base class for FTMO-compliant strategies.

Provides:
- Circuit breakers (daily loss + total drawdown limits)
- Trade count enforcement (max open trades, max daily trades)
- Common order management (enter, SL, TP, notify_order, notify_trade)
"""

import backtrader as bt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config


class FTMOBase(bt.Strategy):
    params = (
        ("daily_loss_limit", 0.04),
        ("total_dd_limit", 0.08),
        ("use_circuit_breaker", False),
        ("max_open_trades", config.MAX_OPEN_TRADES),
        ("max_daily_trades", config.MAX_DAILY_TRADES),
        ("max_lot_size", 5_000_000),  # 50 standard lots absolute cap
    )

    def _init_ftmo(self):
        """Call at the end of subclass __init__."""
        self._initial_balance = config.INITIAL_BALANCE
        self._day_start_equity = self._initial_balance
        self._peak_equity = self._initial_balance
        self._current_day = None
        self._daily_halted = False
        self._total_halted = False
        self._trades_today = 0
        self._open_positions = 0

        # Shared order tracking
        self.orders = {}
        self.stop_orders = {}
        self.tp_orders = {}

        for d in self.datas:
            name = d._name
            self.orders[name] = None
            self.stop_orders[name] = None
            self.tp_orders[name] = None

    def _can_trade(self, d=None):
        """Check all trading preconditions. Returns True if a new trade is allowed."""
        # Circuit breaker check
        if not self._check_ftmo_limits():
            return False

        # Max daily trades
        if self._trades_today >= self.p.max_daily_trades:
            return False

        # Max open positions
        if self._open_positions >= self.p.max_open_trades:
            return False

        # Per-pair checks
        if d is not None:
            name = d._name
            if self.getposition(d).size != 0:
                return False
            if self.orders.get(name):
                return False

        return True

    def _check_ftmo_limits(self):
        """Check circuit breakers. Returns True if trading is allowed."""
        if not self.p.use_circuit_breaker:
            return True

        equity = self.broker.getvalue()
        dt = self.datas[0].datetime.datetime(0)
        today = dt.date()

        if today != self._current_day:
            self._current_day = today
            self._day_start_equity = equity
            self._daily_halted = False
            self._trades_today = 0

        if equity > self._peak_equity:
            self._peak_equity = equity

        total_dd = (self._initial_balance - equity) / self._initial_balance
        if total_dd >= self.p.total_dd_limit:
            self._total_halted = True
            return False

        daily_loss = (self._day_start_equity - equity) / self._day_start_equity
        if daily_loss >= self.p.daily_loss_limit:
            self._daily_halted = True
            return False

        return True

    def _update_day(self):
        """Call at start of next() to track daily state."""
        dt = self.datas[0].datetime.datetime(0)
        today = dt.date()
        if today != self._current_day:
            self._current_day = today
            self._trades_today = 0
            if self.p.use_circuit_breaker:
                self._day_start_equity = self.broker.getvalue()
                self._daily_halted = False

    def _enter_trade(self, d, name, is_long, price, sl, tp, sl_distance):
        """Calculate position size and enter trade with SL/TP."""
        account_value = self.broker.getvalue()
        risk_pct = getattr(self.p, 'risk_pct', config.RISK_PER_TRADE_PCT)
        risk_amount = account_value * risk_pct

        if sl_distance <= 0:
            return

        size = risk_amount / sl_distance

        # Absolute cap to prevent blowup
        size = min(size, self.p.max_lot_size)

        if size <= 0:
            return

        self._trades_today += 1
        self._open_positions += 1

        if is_long:
            self.orders[name] = self.buy(data=d, size=size)
            self.stop_orders[name] = self.sell(
                data=d, size=size, exectype=bt.Order.Stop, price=sl
            )
            self.tp_orders[name] = self.sell(
                data=d, size=size, exectype=bt.Order.Limit, price=tp
            )
        else:
            self.orders[name] = self.sell(data=d, size=size)
            self.stop_orders[name] = self.buy(
                data=d, size=size, exectype=bt.Order.Stop, price=sl
            )
            self.tp_orders[name] = self.buy(
                data=d, size=size, exectype=bt.Order.Limit, price=tp
            )

    def _close_position(self, d):
        """Close position and cancel pending SL/TP orders for a data feed."""
        name = d._name
        if self.getposition(d).size != 0:
            self.close(data=d)
        if self.stop_orders.get(name):
            self.cancel(self.stop_orders[name])
            self.stop_orders[name] = None
        if self.tp_orders.get(name):
            self.cancel(self.tp_orders[name])
            self.tp_orders[name] = None

    def _close_all_positions(self):
        """Emergency close all open positions."""
        for d in self.datas:
            self._close_position(d)

    def notify_order(self, order):
        if order.status in [order.Completed, order.Canceled, order.Margin, order.Rejected]:
            for name in self.orders:
                if self.orders[name] == order:
                    self.orders[name] = None
                    break

    def notify_trade(self, trade):
        if trade.isclosed:
            name = trade.data._name
            self._open_positions = max(0, self._open_positions - 1)
            if self.stop_orders.get(name):
                self.cancel(self.stop_orders[name])
                self.stop_orders[name] = None
            if self.tp_orders.get(name):
                self.cancel(self.tp_orders[name])
                self.tp_orders[name] = None
