"""NY Session Momentum Strategy.

Trades momentum continuation during the London/NY overlap.
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
        self.current_date = {}
        self.rsi = {}
        self.atr = {}

        for d in self.datas:
            name = d._name
            self.london_highs[name] = 0
            self.london_lows[name] = float("inf")
            self.traded_today[name] = False
            self.current_date[name] = None
            self.rsi[name] = bt.indicators.RSI(d.close, period=self.p.rsi_period)
            self.atr[name] = bt.indicators.ATR(d, period=14)

    def next(self):
        self._update_day()
        if self.p.use_circuit_breaker and not self._check_ftmo_limits():
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

        if today != self.current_date.get(name):
            self.london_highs[name] = 0
            self.london_lows[name] = float("inf")
            self.traded_today[name] = False
            self.current_date[name] = today

        # Build London session range
        if self.p.london_start <= hour < self.p.ny_entry_start:
            self.london_highs[name] = max(self.london_highs[name], d.high[0])
            self.london_lows[name] = min(self.london_lows[name], d.low[0])
            return

        # NY entry window
        if self.p.ny_entry_start <= hour < self.p.ny_entry_end:
            if self.traded_today[name]:
                return
            if not self._can_trade(d):
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

            # Long
            if price > london_high and rsi > self.p.rsi_long_threshold:
                sl_distance = min(atr_val * 1.5, self.p.max_sl_pips * pip_size)
                sl = price - sl_distance
                tp = price + (sl_distance * self.p.risk_reward)
                self.traded_today[name] = True
                self._enter_trade(d, name, True, price, sl, tp, sl_distance)

            # Short
            elif price < london_low and rsi < self.p.rsi_short_threshold:
                sl_distance = min(atr_val * 1.5, self.p.max_sl_pips * pip_size)
                sl = price + sl_distance
                tp = price - (sl_distance * self.p.risk_reward)
                self.traded_today[name] = True
                self._enter_trade(d, name, False, price, sl, tp, sl_distance)

        # Session close
        if hour >= self.p.session_close:
            self._close_position(d)
