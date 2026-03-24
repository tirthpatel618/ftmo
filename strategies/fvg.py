"""ICT Fair Value Gap (FVG) Strategy.

Detects 3-candle Fair Value Gap patterns (imbalance zones) and enters
when price retraces to fill the gap. Based on ICT (Inner Circle Trader)
methodology.

A bullish FVG occurs when candle 3's low is above candle 1's high — the gap
between them is an area of imbalance that price tends to revisit.

Documented 55-70% win rate with proper filtering.
"""

import backtrader as bt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from strategies.ftmo_base import FTMOBase


class FVG(FTMOBase):
    params = (
        ("risk_reward", 2.0),           # was 2.5 — more wins at 2.0
        ("risk_pct", config.RISK_PER_TRADE_PCT),
        ("max_sl_pips", 40),
        ("min_impulse_atr_mult", 1.3),  # impulse candle body >= 1.3x ATR
        ("max_fvg_age", 48),            # FVG expires after 48 bars (12 hours on 15min)
        ("max_open_fvgs", 10),          # track up to 10 open FVGs per pair
        ("atr_period", 14),
        ("session_start", 7),
        ("session_end", 20),
        ("ema_period", 50),             # trend filter
        ("min_gap_atr_ratio", 0.3),     # FVG gap must be >= 0.3x ATR to filter noise
    )

    def __init__(self):
        self._init_ftmo()
        self.atr = {}
        self.ema = {}
        self.fvgs = {}  # {pair_name: [list of FVG dicts]}
        self.traded_today = {}
        self.current_date = {}

        for d in self.datas:
            name = d._name
            self.atr[name] = bt.indicators.ATR(d, period=self.p.atr_period)
            self.ema[name] = bt.indicators.ExponentialMovingAverage(
                d.close, period=self.p.ema_period
            )
            self.fvgs[name] = []
            self.traded_today[name] = False
            self.current_date[name] = None

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

        # Need at least 3 bars of history
        if len(d) < 3:
            return

        atr_val = self.atr[name][0]
        if atr_val <= 0:
            return

        price = d.close[0]

        # --- Detect new FVGs ---
        # Candle indices: -2 (oldest), -1 (middle/impulse), 0 (newest)
        c1_high = d.high[-2]
        c1_low = d.low[-2]
        c2_high = d.high[-1]  # impulse candle
        c2_low = d.low[-1]
        c2_body = abs(d.close[-1] - d.open[-1])
        c3_high = d.high[0]
        c3_low = d.low[0]

        min_gap = atr_val * self.p.min_gap_atr_ratio

        # Bullish FVG: gap between candle 1 high and candle 3 low
        if c3_low > c1_high and c2_body >= atr_val * self.p.min_impulse_atr_mult:
            gap = c3_low - c1_high
            if gap >= min_gap and len(self.fvgs[name]) < self.p.max_open_fvgs:
                self.fvgs[name].append({
                    "type": "bullish",
                    "top": c3_low,      # top of gap
                    "bottom": c1_high,  # bottom of gap
                    "age": 0,
                })

        # Bearish FVG: gap between candle 3 high and candle 1 low
        if c3_high < c1_low and c2_body >= atr_val * self.p.min_impulse_atr_mult:
            gap = c1_low - c3_high
            if gap >= min_gap and len(self.fvgs[name]) < self.p.max_open_fvgs:
                self.fvgs[name].append({
                    "type": "bearish",
                    "top": c1_low,      # top of gap
                    "bottom": c3_high,  # bottom of gap
                    "age": 0,
                })

        # --- Age and expire FVGs ---
        active_fvgs = []
        for fvg in self.fvgs[name]:
            fvg["age"] += 1
            if fvg["age"] <= self.p.max_fvg_age:
                active_fvgs.append(fvg)
        self.fvgs[name] = active_fvgs

        # --- Check for FVG fill entries ---
        if hour < self.p.session_start or hour >= self.p.session_end:
            return
        if self.traded_today[name]:
            return
        if not self._can_trade(d):
            return

        ema_val = self.ema[name][0]

        # Look for price entering an FVG zone
        for fvg in self.fvgs[name]:
            if fvg["age"] < 2:
                continue  # skip brand new FVGs

            gap_size = fvg["top"] - fvg["bottom"]
            if gap_size <= 0:
                continue

            if fvg["type"] == "bullish" and price > ema_val:
                # Price retraces down into bullish FVG = buy
                if fvg["bottom"] <= price <= fvg["top"]:
                    sl_distance = min(gap_size + atr_val * 0.5, self.p.max_sl_pips * pip_size)
                    sl = price - sl_distance
                    tp = price + (sl_distance * self.p.risk_reward)
                    self.traded_today[name] = True
                    self.fvgs[name].remove(fvg)
                    self._enter_trade(d, name, True, price, sl, tp, sl_distance)
                    return

            elif fvg["type"] == "bearish" and price < ema_val:
                # Price retraces up into bearish FVG = sell
                if fvg["bottom"] <= price <= fvg["top"]:
                    sl_distance = min(gap_size + atr_val * 0.5, self.p.max_sl_pips * pip_size)
                    sl = price + sl_distance
                    tp = price - (sl_distance * self.p.risk_reward)
                    self.traded_today[name] = True
                    self.fvgs[name].remove(fvg)
                    self._enter_trade(d, name, False, price, sl, tp, sl_distance)
                    return
