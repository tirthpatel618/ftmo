"""Multi-Timeframe London Breakout Strategy.

Same core logic as London Breakout, but adds an H4 EMA trend filter.
Only takes longs when H4 EMA50 is rising (price above), shorts when falling.

This alone should push win rate from ~45% to 55%+ by filtering counter-trend
entries that are the primary source of losses in the base strategy.

Requires H4 resampled data feeds (added by backtester).
"""

import backtrader as bt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from strategies.ftmo_base import FTMOBase


class MTFLondonBreakout(FTMOBase):
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
        ("day_filter", config.LB_DAY_FILTER),
        ("h4_ema_period", 50),
        # When True, use simple price-vs-EMA on 15min (200 period) as fallback
        # when H4 feeds aren't available
        ("use_15min_fallback", True),
        ("ema_15min_period", 200),
    )

    def __init__(self):
        self._init_ftmo()
        self.asian_highs = {}
        self.asian_lows = {}
        self.traded_today = {}
        self.current_date = {}
        self.h4_ema = {}
        self.ema_15min = {}

        # Separate 15min feeds from H4 feeds
        self._15min_feeds = []
        self._h4_feeds = {}

        for d in self.datas:
            name = d._name
            # H4 feeds are named like "EURUSD_H4"
            if name.endswith("_H4"):
                base_name = name.replace("_H4", "")
                self._h4_feeds[base_name] = d
                self.h4_ema[base_name] = bt.indicators.ExponentialMovingAverage(
                    d.close, period=self.p.h4_ema_period
                )
            else:
                self._15min_feeds.append(d)
                self.asian_highs[name] = 0
                self.asian_lows[name] = float("inf")
                self.traded_today[name] = False
                self.current_date[name] = None
                self.ema_15min[name] = bt.indicators.ExponentialMovingAverage(
                    d.close, period=self.p.ema_15min_period
                )

    def next(self):
        self._update_day()
        if self.p.use_circuit_breaker and not self._check_ftmo_limits():
            self._close_all_positions()
            return
        for d in self._15min_feeds:
            self._process_bar(d)

    def _get_pip_size(self, name):
        return config.PIP_SIZES.get(name, 0.0001)

    def _get_trend(self, name, price):
        """Returns 1 for bullish, -1 for bearish, 0 for no filter.

        Uses H4 EMA if available, falls back to 15min EMA.
        """
        if name in self.h4_ema:
            ema_val = self.h4_ema[name][0]
            return 1 if price > ema_val else -1

        if self.p.use_15min_fallback and name in self.ema_15min:
            ema_val = self.ema_15min[name][0]
            return 1 if price > ema_val else -1

        return 0  # no filter

    def _process_bar(self, d):
        name = d._name
        dt = d.datetime.datetime(0)
        hour = dt.hour
        today = dt.date()
        pip_size = self._get_pip_size(name)

        if today != self.current_date.get(name):
            self.asian_highs[name] = 0
            self.asian_lows[name] = float("inf")
            self.traded_today[name] = False
            self.current_date[name] = today

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
            if not self._can_trade(d):
                return

            asian_high = self.asian_highs[name]
            asian_low = self.asian_lows[name]

            if asian_high == 0 or asian_low == float("inf"):
                return

            range_pips = (asian_high - asian_low) / pip_size

            if range_pips < self.p.min_range_pips or range_pips > self.p.max_range_pips:
                return

            price = d.close[0]
            trend = self._get_trend(name, price)

            # Breakout above Asian high + bullish trend
            if price > asian_high and trend >= 0:  # trend 0 = no filter, 1 = bullish
                if trend == -1:
                    return  # bearish trend, skip long

                sl = asian_low
                sl_distance = price - sl
                sl_pips = sl_distance / pip_size

                if sl_pips > self.p.max_sl_pips:
                    sl = price - (self.p.max_sl_pips * pip_size)
                    sl_distance = price - sl

                tp = price + (sl_distance * self.p.risk_reward)
                self.traded_today[name] = True
                self._enter_trade(d, name, True, price, sl, tp, sl_distance)

            # Breakout below Asian low + bearish trend
            elif price < asian_low and trend <= 0:
                if trend == 1:
                    return  # bullish trend, skip short

                sl = asian_high
                sl_distance = sl - price
                sl_pips = sl_distance / pip_size

                if sl_pips > self.p.max_sl_pips:
                    sl = price + (self.p.max_sl_pips * pip_size)
                    sl_distance = sl - price

                tp = price - (sl_distance * self.p.risk_reward)
                self.traded_today[name] = True
                self._enter_trade(d, name, False, price, sl, tp, sl_distance)

        # Session close
        if hour >= self.p.session_close:
            self._close_position(d)
