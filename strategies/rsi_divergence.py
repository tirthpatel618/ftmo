"""RSI Divergence Strategy.

Detects bullish and bearish divergences between price and RSI, then enters
on confirmation. Divergence = momentum weakening while price continues,
signaling a likely reversal.

- Bullish divergence: price makes lower low, RSI makes higher low
- Bearish divergence: price makes higher high, RSI makes lower high

Documented 52-73% win rate with proper filtering.
"""

import backtrader as bt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from strategies.ftmo_base import FTMOBase


class RSIDivergence(FTMOBase):
    params = (
        ("rsi_period", 14),
        ("rsi_extreme_low", 40),     # was 35 — widened to catch more divergences
        ("rsi_extreme_high", 60),    # was 65
        ("swing_lookback", 3),       # was 5 — smaller = more swings detected
        ("max_swing_distance", 60),  # was 50 — allow slightly older divergences
        ("risk_reward", 2.0),
        ("risk_pct", config.RISK_PER_TRADE_PCT),
        ("max_sl_pips", 40),
        ("atr_period", 14),
        ("atr_sl_mult", 1.5),
        ("session_start", 7),
        ("session_end", 20),
    )

    def __init__(self):
        self._init_ftmo()
        self.rsi = {}
        self.atr = {}
        self.traded_today = {}
        self.current_date = {}
        # Track recent swing lows/highs for divergence detection
        self.price_swing_lows = {}   # [(bar_index, price)]
        self.price_swing_highs = {}
        self.rsi_swing_lows = {}     # [(bar_index, rsi_value)]
        self.rsi_swing_highs = {}

        for d in self.datas:
            name = d._name
            self.rsi[name] = bt.indicators.RSI(d.close, period=self.p.rsi_period)
            self.atr[name] = bt.indicators.ATR(d, period=self.p.atr_period)
            self.traded_today[name] = False
            self.current_date[name] = None
            self.price_swing_lows[name] = []
            self.price_swing_highs[name] = []
            self.rsi_swing_lows[name] = []
            self.rsi_swing_highs[name] = []

    def next(self):
        self._update_day()
        if self.p.use_circuit_breaker and not self._check_ftmo_limits():
            self._close_all_positions()
            return
        for d in self.datas:
            self._process_bar(d)

    def _get_pip_size(self, name):
        return config.PIP_SIZES.get(name, 0.0001)

    def _is_swing_low(self, d, offset, lookback):
        """Check if bar at -offset is a swing low (lower than lookback bars on each side)."""
        try:
            val = d.low[-offset]
            for i in range(1, lookback + 1):
                if d.low[-offset - i] <= val:
                    return False
                if d.low[-offset + i] <= val:
                    return False
            return True
        except IndexError:
            return False

    def _is_swing_high(self, d, offset, lookback):
        """Check if bar at -offset is a swing high."""
        try:
            val = d.high[-offset]
            for i in range(1, lookback + 1):
                if d.high[-offset - i] >= val:
                    return False
                if d.high[-offset + i] >= val:
                    return False
            return True
        except IndexError:
            return False

    def _process_bar(self, d):
        name = d._name
        dt = d.datetime.datetime(0)
        hour = dt.hour
        today = dt.date()
        pip_size = self._get_pip_size(name)
        lb = self.p.swing_lookback

        if today != self.current_date.get(name):
            self.traded_today[name] = False
            self.current_date[name] = today

        # Need enough history for swing detection
        if len(d) < lb * 2 + self.p.max_swing_distance + 1:
            return

        bar_index = len(d)
        atr_val = self.atr[name][0]
        rsi_val = self.rsi[name][0]

        if atr_val <= 0:
            return

        # Detect swings at -lb offset (confirmed swing, has lb bars on each side)
        if self._is_swing_low(d, lb, lb):
            self.price_swing_lows[name].append((bar_index - lb, d.low[-lb]))
            self.rsi_swing_lows[name].append((bar_index - lb, self.rsi[name][-lb]))
            # Keep only recent swings
            self.price_swing_lows[name] = self.price_swing_lows[name][-10:]
            self.rsi_swing_lows[name] = self.rsi_swing_lows[name][-10:]

        if self._is_swing_high(d, lb, lb):
            self.price_swing_highs[name].append((bar_index - lb, d.high[-lb]))
            self.rsi_swing_highs[name].append((bar_index - lb, self.rsi[name][-lb]))
            self.price_swing_highs[name] = self.price_swing_highs[name][-10:]
            self.rsi_swing_highs[name] = self.rsi_swing_highs[name][-10:]

        # Only trade during active sessions
        if hour < self.p.session_start or hour >= self.p.session_end:
            return
        if self.traded_today[name]:
            return
        if not self._can_trade(d):
            return

        price = d.close[0]

        # --- Check for bullish divergence ---
        # Price lower low + RSI higher low + RSI in oversold zone
        if len(self.price_swing_lows[name]) >= 2:
            prev_bar, prev_price = self.price_swing_lows[name][-2]
            curr_bar, curr_price = self.price_swing_lows[name][-1]
            prev_rsi = self.rsi_swing_lows[name][-2][1]
            curr_rsi = self.rsi_swing_lows[name][-1][1]

            distance = curr_bar - prev_bar
            if (distance <= self.p.max_swing_distance and
                    curr_price < prev_price and          # price lower low
                    curr_rsi > prev_rsi and              # RSI higher low
                    curr_rsi < self.p.rsi_extreme_low):  # RSI oversold

                sl_distance = min(atr_val * self.p.atr_sl_mult, self.p.max_sl_pips * pip_size)
                sl = price - sl_distance
                tp = price + (sl_distance * self.p.risk_reward)
                self.traded_today[name] = True
                self._enter_trade(d, name, True, price, sl, tp, sl_distance)
                return

        # --- Check for bearish divergence ---
        # Price higher high + RSI lower high + RSI in overbought zone
        if len(self.price_swing_highs[name]) >= 2:
            prev_bar, prev_price = self.price_swing_highs[name][-2]
            curr_bar, curr_price = self.price_swing_highs[name][-1]
            prev_rsi = self.rsi_swing_highs[name][-2][1]
            curr_rsi = self.rsi_swing_highs[name][-1][1]

            distance = curr_bar - prev_bar
            if (distance <= self.p.max_swing_distance and
                    curr_price > prev_price and          # price higher high
                    curr_rsi < prev_rsi and              # RSI lower high
                    curr_rsi > self.p.rsi_extreme_high): # RSI overbought

                sl_distance = min(atr_val * self.p.atr_sl_mult, self.p.max_sl_pips * pip_size)
                sl = price + sl_distance
                tp = price - (sl_distance * self.p.risk_reward)
                self.traded_today[name] = True
                self._enter_trade(d, name, False, price, sl, tp, sl_distance)
                return
