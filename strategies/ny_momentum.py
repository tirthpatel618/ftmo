"""NY Session Momentum Strategy.

Trades momentum continuation during the London/NY overlap.
- Entry: Break of London session high/low after 12:00 UTC
- Confirmed by RSI momentum filter
- Pairs: EUR/USD, GBP/USD, USD/CAD
"""

import backtrader as bt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from strategies.ftmo_base import FTMOBase


class NYMomentum(FTMOBase):
    params = (
        ("london_start", config.LONDON_OPEN),
        ("ny_entry_start", config.NY_OPEN),
        ("ny_entry_end", config.NY_ENTRY_END),
        ("session_close", config.SESSION_CLOSE),
        ("rsi_period", config.NY_RSI_PERIOD),
        ("rsi_long_threshold", config.NY_RSI_LONG_THRESHOLD),
        ("rsi_short_threshold", config.NY_RSI_SHORT_THRESHOLD),
        ("risk_reward", config.NY_RISK_REWARD),
        ("risk_pct", config.RISK_PER_TRADE_PCT),
        ("max_sl_pips", 40),
    )

    def __init__(self):
        self._init_ftmo()

        self.london_highs = {}
        self.london_lows = {}
        self.traded_today = {}
        self.orders = {}
        self.stop_orders = {}
        self.tp_orders = {}
        self.current_date = {}
        self.rsi = {}
        self.atr = {}

        for d in self.datas:
            name = d._name
            self.london_highs[name] = 0
            self.london_lows[name] = float("inf")
            self.traded_today[name] = False
            self.orders[name] = None
            self.stop_orders[name] = None
            self.tp_orders[name] = None
            self.current_date[name] = None
            self.rsi[name] = bt.indicators.RSI(d.close, period=self.p.rsi_period)
            self.atr[name] = bt.indicators.ATR(d, period=14)

    def next(self):
        if not self._check_ftmo_limits():
            self._close_all_positions()
            return
        for d in self.datas:
            self._process_bar(d)

    def _get_pip_size(self, name):
        return config.PIP_SIZES.get(name, 0.0001)

    def _process_bar(self, d):
        name = d._name
        dt = d.datetime.datetime(0)
        hour = dt.hour
        today = dt.date()
        pip_size = self._get_pip_size(name)

        # New day — reset
        if today != self.current_date.get(name):
            self.london_highs[name] = 0
            self.london_lows[name] = float("inf")
            self.traded_today[name] = False
            self.current_date[name] = today

        # Build London session range (07:00-12:00 UTC)
        if self.p.london_start <= hour < self.p.ny_entry_start:
            self.london_highs[name] = max(self.london_highs[name], d.high[0])
            self.london_lows[name] = min(self.london_lows[name], d.low[0])
            return

        # NY entry window
        if self.p.ny_entry_start <= hour < self.p.ny_entry_end:
            if self.traded_today[name]:
                return
            if self.getposition(d).size != 0:
                return
            if self.orders.get(name):
                return

            london_high = self.london_highs[name]
            london_low = self.london_lows[name]

            if london_high == 0 or london_low == float("inf"):
                return

            price = d.close[0]
            rsi = self.rsi[name][0]
            atr_val = self.atr[name][0]

            if atr_val <= 0:
                return

            # Long: break above London high + RSI confirms momentum
            if price > london_high and rsi > self.p.rsi_long_threshold:
                sl_distance = min(atr_val * 1.5, self.p.max_sl_pips * pip_size)
                sl = price - sl_distance
                tp = price + (sl_distance * self.p.risk_reward)
                self._enter_trade(d, name, True, price, sl, tp, sl_distance)

            # Short: break below London low + RSI confirms weakness
            elif price < london_low and rsi < self.p.rsi_short_threshold:
                sl_distance = min(atr_val * 1.5, self.p.max_sl_pips * pip_size)
                sl = price + sl_distance
                tp = price - (sl_distance * self.p.risk_reward)
                self._enter_trade(d, name, False, price, sl, tp, sl_distance)

        # Session close
        if hour >= self.p.session_close:
            if self.getposition(d).size != 0:
                self.close(data=d)
                if self.stop_orders.get(name):
                    self.cancel(self.stop_orders[name])
                if self.tp_orders.get(name):
                    self.cancel(self.tp_orders[name])

    def _enter_trade(self, d, name, is_long, price, sl, tp, sl_distance):
        account_value = self.broker.getvalue()
        risk_amount = account_value * self.p.risk_pct
        size = risk_amount / sl_distance

        if size <= 0:
            return

        self.traded_today[name] = True

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

    def notify_order(self, order):
        if order.status in [order.Completed, order.Canceled, order.Margin, order.Rejected]:
            for name in self.orders:
                if self.orders[name] == order:
                    self.orders[name] = None
                    break

    def notify_trade(self, trade):
        if trade.isclosed:
            name = trade.data._name
            if self.stop_orders.get(name):
                self.cancel(self.stop_orders[name])
                self.stop_orders[name] = None
            if self.tp_orders.get(name):
                self.cancel(self.tp_orders[name])
                self.tp_orders[name] = None
