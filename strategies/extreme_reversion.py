"""Extreme Move Reversion Strategy (Homegrown).

Based on pattern scanner finding: after 2σ+ moves on range-bound pairs,
price reverts 57-61% of the time within the next 1-4 bars.

Edge is strongest on AUDNZD and EURCHF (structurally range-bound pairs).
At |z| > 2.5, reversion rate hits 58-61% with ~0.6-1.2 pip average reversion.

Logic:
1. Track rolling mean and stddev of 15min returns (100-bar window)
2. When a bar's return exceeds threshold (default 2.0σ), enter counter-trend
3. Target: partial reversion (1.0-1.5x the extreme move's size)
4. SL: beyond the extreme candle's wick + buffer
5. Session filter: active hours only (avoid illiquid Asian)
"""

import backtrader as bt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from strategies.ftmo_base import FTMOBase


class ExtremeReversion(FTMOBase):
    params = (
        ("lookback", 40),             # was 100 — shorter window = more sensitive z-scores
        ("z_threshold", 1.5),         # was 2.0 — lower for more trades
        ("risk_reward", 2.0),
        ("risk_pct", config.RISK_PER_TRADE_PCT),
        ("max_sl_pips", 30),
        ("atr_period", 14),
        ("sl_buffer_atr", 0.5),       # was 0.3 — wider buffer to avoid instant SL hits
        ("session_start", 3),         # include late Asian for range pairs
        ("session_end", 20),
        ("cooldown_bars", 2),
        ("max_open_trades", 6),       # override base — allow all 6 pairs to have positions
        ("max_daily_trades", 8),      # override base — reversion signals cluster
    )

    def __init__(self):
        self._init_ftmo()
        self.atr = {}
        self.traded_today = {}
        self.current_date = {}
        self.bars_since_trade = {}
        # We'll compute rolling stats manually since backtrader's
        # built-in indicators don't expose z-scores directly
        self.return_history = {}

        for d in self.datas:
            name = d._name
            self.atr[name] = bt.indicators.ATR(d, period=self.p.atr_period)
            self.traded_today[name] = False
            self.current_date[name] = None
            self.bars_since_trade[name] = 999
            self.return_history[name] = []

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
            self.traded_today[name] = False
            self.current_date[name] = today

        self.bars_since_trade[name] += 1

        # Calculate bar return
        if d.open[0] == 0:
            return
        bar_return = (d.close[0] - d.open[0]) / d.open[0]

        # Maintain rolling history
        self.return_history[name].append(bar_return)
        if len(self.return_history[name]) > self.p.lookback:
            self.return_history[name] = self.return_history[name][-self.p.lookback:]

        # Need enough history
        if len(self.return_history[name]) < self.p.lookback:
            return

        atr_val = self.atr[name][0]
        if atr_val <= 0:
            return

        # Calculate z-score
        hist = self.return_history[name]
        mean_ret = sum(hist) / len(hist)
        var = sum((r - mean_ret) ** 2 for r in hist) / len(hist)
        std_ret = var ** 0.5

        if std_ret == 0:
            return

        z_score = (bar_return - mean_ret) / std_ret

        # Session filter
        if hour < self.p.session_start or hour >= self.p.session_end:
            return

        if not self._can_trade(d):
            return

        # Cooldown
        if self.bars_since_trade[name] < self.p.cooldown_bars:
            return

        price = d.close[0]

        # Extreme UP move → SHORT (fade it)
        if z_score > self.p.z_threshold:
            # SL above the extreme candle's high + buffer
            sl = d.high[0] + atr_val * self.p.sl_buffer_atr
            sl_distance = sl - price
            sl_pips = sl_distance / pip_size

            if sl_pips > self.p.max_sl_pips:
                sl = price + (self.p.max_sl_pips * pip_size)
                sl_distance = sl - price

            if sl_distance <= 0:
                return

            tp = price - (sl_distance * self.p.risk_reward)
            self.bars_since_trade[name] = 0
            self._enter_trade(d, name, False, price, sl, tp, sl_distance)

        # Extreme DOWN move → LONG (fade it)
        elif z_score < -self.p.z_threshold:
            # SL below the extreme candle's low - buffer
            sl = d.low[0] - atr_val * self.p.sl_buffer_atr
            sl_distance = price - sl
            sl_pips = sl_distance / pip_size

            if sl_pips > self.p.max_sl_pips:
                sl = price - (self.p.max_sl_pips * pip_size)
                sl_distance = price - sl

            if sl_distance <= 0:
                return

            tp = price + (sl_distance * self.p.risk_reward)
            self.bars_since_trade[name] = 0
            self._enter_trade(d, name, True, price, sl, tp, sl_distance)
