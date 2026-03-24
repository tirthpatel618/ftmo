"""Ensemble Strategy — prioritized multi-strategy system.

Priority order:
1. Extreme Reversion — highest conviction, always take when signaled
2. London Breakout — primary during London session (7-11 UTC)
3. BB Squeeze — fills gaps throughout the day (7-20 UTC)

Only one strategy can have an active position per pair at a time.
Daily trade budget is shared across all sub-strategies.
"""

import backtrader as bt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from strategies.ftmo_base import FTMOBase


class Ensemble(FTMOBase):
    params = (
        # Shared
        ("risk_pct", config.RISK_PER_TRADE_PCT),
        ("session_close", config.SESSION_CLOSE),

        # Extreme Reversion params
        ("er_lookback", 40),
        ("er_z_threshold", 1.5),
        ("er_risk_reward", 2.0),
        ("er_max_sl_pips", 30),
        ("er_sl_buffer_atr", 0.5),
        ("er_session_start", 3),
        ("er_session_end", 20),
        ("er_cooldown_bars", 2),

        # London Breakout params
        ("lb_asian_start", config.ASIAN_SESSION_START),
        ("lb_asian_end", config.ASIAN_SESSION_END),
        ("lb_entry_start", config.LONDON_OPEN),
        ("lb_entry_end", config.LONDON_ENTRY_END),
        ("lb_min_range_pips", config.LB_MIN_RANGE_PIPS),
        ("lb_max_range_pips", config.LB_MAX_RANGE_PIPS),
        ("lb_max_sl_pips", config.LB_MAX_SL_PIPS),
        ("lb_risk_reward", config.LB_RISK_REWARD),
        ("lb_day_filter", config.LB_DAY_FILTER),

        # BB Squeeze params
        ("sq_bb_period", 20),
        ("sq_bb_std", 2.0),
        ("sq_kc_period", 20),
        ("sq_kc_atr_mult", 1.5),
        ("sq_squeeze_min_bars", 4),
        ("sq_release_window", 6),
        ("sq_risk_reward", 2.0),
        ("sq_max_sl_pips", 40),
        ("sq_session_start", 7),
        ("sq_session_end", 20),
        ("sq_min_body_ratio", 0.5),

        ("atr_period", 14),
    )

    def __init__(self):
        self._init_ftmo()

        # Shared indicators
        self.atr = {}

        # Extreme Reversion state
        self.return_history = {}
        self.er_cooldown = {}

        # London Breakout state
        self.asian_highs = {}
        self.asian_lows = {}
        self.lb_traded_today = {}

        # BB Squeeze state
        self.bb = {}
        self.kc_upper = {}
        self.kc_lower = {}
        self.ema = {}
        self.squeeze_count = {}
        self.bars_since_release = {}
        self.last_squeeze_bars = {}
        self.sq_traded_today = {}

        # Track which strategy is active per pair
        self.active_strategy = {}  # pair -> "er" | "lb" | "sq" | None
        self.current_date = {}
        self.trade_source = {}  # track which strategy opened each trade

        for d in self.datas:
            name = d._name
            self.atr[name] = bt.indicators.ATR(d, period=self.p.atr_period)

            # ER state
            self.return_history[name] = []
            self.er_cooldown[name] = 999

            # LB state
            self.asian_highs[name] = 0
            self.asian_lows[name] = float("inf")
            self.lb_traded_today[name] = False

            # SQ state
            self.bb[name] = bt.indicators.BollingerBands(
                d.close, period=self.p.sq_bb_period, devfactor=self.p.sq_bb_std
            )
            sq_ema = bt.indicators.ExponentialMovingAverage(d.close, period=self.p.sq_kc_period)
            self.ema[name] = sq_ema
            self.kc_upper[name] = sq_ema + self.atr[name] * self.p.sq_kc_atr_mult
            self.kc_lower[name] = sq_ema - self.atr[name] * self.p.sq_kc_atr_mult
            self.squeeze_count[name] = 0
            self.bars_since_release[name] = 999
            self.last_squeeze_bars[name] = 0
            self.sq_traded_today[name] = False

            self.active_strategy[name] = None
            self.current_date[name] = None
            self.trade_source[name] = None

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

        # Daily reset
        if today != self.current_date.get(name):
            self.asian_highs[name] = 0
            self.asian_lows[name] = float("inf")
            self.lb_traded_today[name] = False
            self.sq_traded_today[name] = False
            self.current_date[name] = today

        atr_val = self.atr[name][0]
        if atr_val <= 0:
            return

        # Update all strategy states regardless of whether we can trade
        self._update_er_state(d, name)
        self._update_lb_state(d, name, hour)
        self._update_sq_state(d, name)

        # Session close — close everything
        if hour >= self.p.session_close:
            self._close_position(d)
            self.active_strategy[name] = None
            return

        # If we already have a position on this pair, don't enter another
        if self.getposition(d).size != 0:
            return

        self.active_strategy[name] = None

        # PRIORITY 1: Extreme Reversion
        er_signal = self._check_er_signal(d, name, hour, pip_size, atr_val)
        if er_signal:
            return

        # PRIORITY 2: London Breakout (only during London entry window)
        lb_signal = self._check_lb_signal(d, name, hour, pip_size, atr_val, dt)
        if lb_signal:
            return

        # PRIORITY 3: BB Squeeze
        sq_signal = self._check_sq_signal(d, name, hour, pip_size, atr_val)
        if sq_signal:
            return

    # ── Extreme Reversion ───────────────────────────────────

    def _update_er_state(self, d, name):
        self.er_cooldown[name] += 1
        if d.open[0] == 0:
            return
        bar_return = (d.close[0] - d.open[0]) / d.open[0]
        self.return_history[name].append(bar_return)
        if len(self.return_history[name]) > self.p.er_lookback:
            self.return_history[name] = self.return_history[name][-self.p.er_lookback:]

    def _check_er_signal(self, d, name, hour, pip_size, atr_val):
        if hour < self.p.er_session_start or hour >= self.p.er_session_end:
            return False
        if len(self.return_history[name]) < self.p.er_lookback:
            return False
        if self.er_cooldown[name] < self.p.er_cooldown_bars:
            return False
        if not self._can_trade(d):
            return False

        hist = self.return_history[name]
        mean_ret = sum(hist) / len(hist)
        var = sum((r - mean_ret) ** 2 for r in hist) / len(hist)
        std_ret = var ** 0.5
        if std_ret == 0:
            return False

        bar_return = hist[-1]
        z_score = (bar_return - mean_ret) / std_ret
        price = d.close[0]

        # Extreme UP → SHORT
        if z_score > self.p.er_z_threshold:
            sl = d.high[0] + atr_val * self.p.er_sl_buffer_atr
            sl_distance = sl - price
            if sl_distance / pip_size > self.p.er_max_sl_pips:
                sl = price + (self.p.er_max_sl_pips * pip_size)
                sl_distance = sl - price
            if sl_distance <= 0:
                return False
            tp = price - (sl_distance * self.p.er_risk_reward)
            self.active_strategy[name] = "er"
            self.er_cooldown[name] = 0
            self._enter_trade(d, name, False, price, sl, tp, sl_distance)
            return True

        # Extreme DOWN → LONG
        if z_score < -self.p.er_z_threshold:
            sl = d.low[0] - atr_val * self.p.er_sl_buffer_atr
            sl_distance = price - sl
            if sl_distance / pip_size > self.p.er_max_sl_pips:
                sl = price - (self.p.er_max_sl_pips * pip_size)
                sl_distance = price - sl
            if sl_distance <= 0:
                return False
            tp = price + (sl_distance * self.p.er_risk_reward)
            self.active_strategy[name] = "er"
            self.er_cooldown[name] = 0
            self._enter_trade(d, name, True, price, sl, tp, sl_distance)
            return True

        return False

    # ── London Breakout ─────────────────────────────────────

    def _update_lb_state(self, d, name, hour):
        if self.p.lb_asian_start <= hour < self.p.lb_asian_end:
            self.asian_highs[name] = max(self.asian_highs[name], d.high[0])
            self.asian_lows[name] = min(self.asian_lows[name], d.low[0])

    def _check_lb_signal(self, d, name, hour, pip_size, atr_val, dt):
        if not (self.p.lb_entry_start <= hour < self.p.lb_entry_end):
            return False
        if self.lb_traded_today[name]:
            return False
        if self.p.lb_day_filter and dt.weekday() not in self.p.lb_day_filter:
            return False
        if not self._can_trade(d):
            return False

        asian_high = self.asian_highs[name]
        asian_low = self.asian_lows[name]
        if asian_high == 0 or asian_low == float("inf"):
            return False

        range_pips = (asian_high - asian_low) / pip_size
        if range_pips < self.p.lb_min_range_pips or range_pips > self.p.lb_max_range_pips:
            return False

        price = d.close[0]

        # Breakout above Asian high → LONG
        if price > asian_high:
            sl = asian_low
            sl_distance = price - sl
            if sl_distance / pip_size > self.p.lb_max_sl_pips:
                sl = price - (self.p.lb_max_sl_pips * pip_size)
                sl_distance = price - sl
            tp = price + (sl_distance * self.p.lb_risk_reward)
            self.lb_traded_today[name] = True
            self.active_strategy[name] = "lb"
            self._enter_trade(d, name, True, price, sl, tp, sl_distance)
            return True

        # Breakout below Asian low → SHORT
        if price < asian_low:
            sl = asian_high
            sl_distance = sl - price
            if sl_distance / pip_size > self.p.lb_max_sl_pips:
                sl = price + (self.p.lb_max_sl_pips * pip_size)
                sl_distance = sl - price
            tp = price - (sl_distance * self.p.lb_risk_reward)
            self.lb_traded_today[name] = True
            self.active_strategy[name] = "lb"
            self._enter_trade(d, name, False, price, sl, tp, sl_distance)
            return True

        return False

    # ── BB Squeeze ──────────────────────────────────────────

    def _update_sq_state(self, d, name):
        bb = self.bb[name]
        kc_up = self.kc_upper[name][0]
        kc_lo = self.kc_lower[name][0]
        bb_up = bb.lines.top[0]
        bb_lo = bb.lines.bot[0]

        in_squeeze = bb_up < kc_up and bb_lo > kc_lo

        if in_squeeze:
            self.squeeze_count[name] += 1
            self.bars_since_release[name] = 999
        else:
            if self.squeeze_count[name] > 0:
                self.last_squeeze_bars[name] = self.squeeze_count[name]
                self.squeeze_count[name] = 0
                self.bars_since_release[name] = 0
            else:
                self.bars_since_release[name] += 1

    def _check_sq_signal(self, d, name, hour, pip_size, atr_val):
        if hour < self.p.sq_session_start or hour >= self.p.sq_session_end:
            return False
        if self.last_squeeze_bars[name] < self.p.sq_squeeze_min_bars:
            return False
        if self.bars_since_release[name] > self.p.sq_release_window:
            return False
        if self.sq_traded_today[name]:
            return False
        if not self._can_trade(d):
            return False

        price = d.close[0]
        bb = self.bb[name]
        bb_up = bb.lines.top[0]
        bb_lo = bb.lines.bot[0]
        ema_val = self.ema[name][0]

        candle_range = d.high[0] - d.low[0]
        candle_body = abs(d.close[0] - d.open[0])
        if candle_range > 0 and candle_body / candle_range < self.p.sq_min_body_ratio:
            return False

        # Breakout above BB + above EMA = long
        if price > bb_up and price > ema_val:
            sl_distance = min(atr_val * 1.5, self.p.sq_max_sl_pips * pip_size)
            sl = price - sl_distance
            tp = price + (sl_distance * self.p.sq_risk_reward)
            self.sq_traded_today[name] = True
            self.active_strategy[name] = "sq"
            self._enter_trade(d, name, True, price, sl, tp, sl_distance)
            return True

        # Breakout below BB + below EMA = short
        if price < bb_lo and price < ema_val:
            sl_distance = min(atr_val * 1.5, self.p.sq_max_sl_pips * pip_size)
            sl = price + sl_distance
            tp = price - (sl_distance * self.p.sq_risk_reward)
            self.sq_traded_today[name] = True
            self.active_strategy[name] = "sq"
            self._enter_trade(d, name, False, price, sl, tp, sl_distance)
            return True

        return False
