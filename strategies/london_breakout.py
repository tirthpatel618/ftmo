"""London Session Breakout Strategy.

Trades the breakout of the Asian session range during the London open.
- Asian range: 00:00-07:00 UTC
- Entry: 15min candle close above Asian high (long) or below Asian low (short)
- Entry window: 07:00-11:00 UTC
- Stop loss: opposite end of Asian range (capped)
- Take profit: R:R based
"""

import backtrader as bt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from strategies.ftmo_base import FTMOBase


class LondonBreakout(FTMOBase):
    params = (
        ("asian_start", config.ASIAN_SESSION_START),
        ("asian_end", config.ASIAN_SESSION_END),
        ("london_entry_start", config.LONDON_OPEN),
        ("london_entry_end", config.LONDON_ENTRY_END),
        ("session_close", config.SESSION_CLOSE),
        ("min_range_pips", config.LB_MIN_RANGE_PIPS),
        ("max_range_pips", config.LB_MAX_RANGE_PIPS),
        ("max_sl_pips", config.LB_MAX_SL_PIPS),
        ("risk_reward", config.LB_RISK_REWARD),
        ("risk_pct", config.RISK_PER_TRADE_PCT),
        ("use_trend_filter", config.LB_USE_TREND_FILTER),
        ("day_filter", config.LB_DAY_FILTER),
    )

    def __init__(self):
        self._init_ftmo()

        self.asian_highs = {}
        self.asian_lows = {}
        self.traded_today = {}
        self.orders = {}
        self.stop_orders = {}
        self.tp_orders = {}
        self.current_date = {}
        self.ema200 = {}

        for d in self.datas:
            name = d._name
            self.asian_highs[name] = 0
            self.asian_lows[name] = float("inf")
            self.traded_today[name] = False
            self.orders[name] = None
            self.stop_orders[name] = None
            self.tp_orders[name] = None
            self.current_date[name] = None
            self.ema200[name] = bt.indicators.ExponentialMovingAverage(
                d.close, period=200
            )

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
            self.asian_highs[name] = 0
            self.asian_lows[name] = float("inf")
            self.traded_today[name] = False
            self.current_date[name] = today

        # Day of week filter (Monday=0, Friday=4)
        if self.p.day_filter and dt.weekday() not in self.p.day_filter:
            return

        # Build Asian range
        if self.p.asian_start <= hour < self.p.asian_end:
            self.asian_highs[name] = max(self.asian_highs[name], d.high[0])
            self.asian_lows[name] = min(self.asian_lows[name], d.low[0])
            return

        # London entry window
        if self.p.london_entry_start <= hour < self.p.london_entry_end:
            if self.traded_today[name]:
                return
            if self.getposition(d).size != 0:
                return
            if self.orders.get(name):
                return

            asian_high = self.asian_highs[name]
            asian_low = self.asian_lows[name]

            if asian_high == 0 or asian_low == float("inf"):
                return

            range_pips = (asian_high - asian_low) / pip_size

            # Range filter
            if range_pips < self.p.min_range_pips or range_pips > self.p.max_range_pips:
                return

            price = d.close[0]

            # Breakout above Asian high → LONG
            if price > asian_high:
                sl = asian_low
                sl_distance = price - sl
                sl_pips = sl_distance / pip_size

                # Cap stop loss
                if sl_pips > self.p.max_sl_pips:
                    sl = price - (self.p.max_sl_pips * pip_size)
                    sl_distance = price - sl

                # Trend filter
                if self.p.use_trend_filter and price < self.ema200[name][0]:
                    return

                tp = price + (sl_distance * self.p.risk_reward)
                self._enter_trade(d, name, True, price, sl, tp, sl_distance)

            # Breakout below Asian low → SHORT
            elif price < asian_low:
                sl = asian_high
                sl_distance = sl - price
                sl_pips = sl_distance / pip_size

                if sl_pips > self.p.max_sl_pips:
                    sl = price + (self.p.max_sl_pips * pip_size)
                    sl_distance = sl - price

                if self.p.use_trend_filter and price > self.ema200[name][0]:
                    return

                tp = price - (sl_distance * self.p.risk_reward)
                self._enter_trade(d, name, False, price, sl, tp, sl_distance)

        # Session close — close any remaining positions
        if hour >= self.p.session_close:
            if self.getposition(d).size != 0:
                self.close(data=d)
                if self.stop_orders.get(name):
                    self.cancel(self.stop_orders[name])
                if self.tp_orders.get(name):
                    self.cancel(self.tp_orders[name])

    def _enter_trade(self, d, name, is_long, price, sl, tp, sl_distance):
        """Calculate position size and enter trade."""
        account_value = self.broker.getvalue()
        risk_amount = account_value * self.p.risk_pct

        if sl_distance <= 0:
            return

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
