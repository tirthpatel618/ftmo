"""Bollinger Band Squeeze Breakout Strategy.

Trades volatility expansion after contraction. When Bollinger Bands contract
inside Keltner Channels (a "squeeze"), volatility is coiling. Enter on the
first breakout candle after the squeeze releases.

Documented 55-65% win rate on trending pairs during active sessions.
"""

import backtrader as bt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from strategies.ftmo_base import FTMOBase


class BBSqueeze(FTMOBase):
    params = (
        ("bb_period", 20),
        ("bb_std", 2.0),
        ("kc_period", 20),
        ("kc_atr_mult", 1.5),
        ("squeeze_min_bars", 4),        # min bars in squeeze before trading (was 6)
        ("release_window", 6),          # bars after squeeze release to look for entry
        ("risk_reward", 2.0),
        ("risk_pct", config.RISK_PER_TRADE_PCT),
        ("max_sl_pips", 40),
        ("session_start", 7),           # London open
        ("session_end", 20),            # NY close
        ("atr_period", 14),
        ("min_body_ratio", 0.5),        # breakout candle body must be >= 50% of range
    )

    def __init__(self):
        self._init_ftmo()
        self.bb = {}
        self.kc_upper = {}
        self.kc_lower = {}
        self.atr = {}
        self.ema = {}
        self.squeeze_count = {}
        self.bars_since_release = {}    # how many bars since squeeze ended
        self.last_squeeze_bars = {}     # how long the last squeeze lasted
        self.traded_today = {}
        self.current_date = {}

        for d in self.datas:
            name = d._name
            self.bb[name] = bt.indicators.BollingerBands(
                d.close, period=self.p.bb_period, devfactor=self.p.bb_std
            )
            # Keltner Channel: EMA +/- ATR * mult
            ema = bt.indicators.ExponentialMovingAverage(d.close, period=self.p.kc_period)
            atr = bt.indicators.ATR(d, period=self.p.atr_period)
            self.atr[name] = atr
            self.ema[name] = ema
            self.kc_upper[name] = ema + atr * self.p.kc_atr_mult
            self.kc_lower[name] = ema - atr * self.p.kc_atr_mult
            self.squeeze_count[name] = 0
            self.bars_since_release[name] = 999
            self.last_squeeze_bars[name] = 0
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

        bb = self.bb[name]
        kc_up = self.kc_upper[name][0]
        kc_lo = self.kc_lower[name][0]
        bb_up = bb.lines.top[0]
        bb_lo = bb.lines.bot[0]
        atr_val = self.atr[name][0]

        if atr_val <= 0:
            return

        # Detect squeeze: BB inside KC
        in_squeeze = bb_up < kc_up and bb_lo > kc_lo

        if in_squeeze:
            self.squeeze_count[name] += 1
            self.bars_since_release[name] = 999  # reset release counter
            return  # Don't trade during squeeze

        # Track squeeze release
        if self.squeeze_count[name] > 0:
            # Squeeze just ended this bar
            self.last_squeeze_bars[name] = self.squeeze_count[name]
            self.squeeze_count[name] = 0
            self.bars_since_release[name] = 0
        else:
            self.bars_since_release[name] += 1

        # Only trade during active sessions
        if hour < self.p.session_start or hour >= self.p.session_end:
            return

        # Must have had a valid squeeze recently
        if self.last_squeeze_bars[name] < self.p.squeeze_min_bars:
            return

        # Entry window: within N bars of squeeze release
        if self.bars_since_release[name] > self.p.release_window:
            return

        if self.traded_today[name]:
            return
        if not self._can_trade(d):
            return

        price = d.close[0]
        candle_range = d.high[0] - d.low[0]
        candle_body = abs(d.close[0] - d.open[0])

        # Momentum confirmation: strong candle body
        if candle_range > 0 and candle_body / candle_range < self.p.min_body_ratio:
            return

        ema_val = self.ema[name][0]

        # Breakout above upper BB = long (with EMA trend confirmation)
        if price > bb_up and price > ema_val:
            sl_distance = min(atr_val * 1.5, self.p.max_sl_pips * pip_size)
            sl = price - sl_distance
            tp = price + (sl_distance * self.p.risk_reward)
            self.traded_today[name] = True
            self._enter_trade(d, name, True, price, sl, tp, sl_distance)

        # Breakout below lower BB = short (with EMA trend confirmation)
        elif price < bb_lo and price < ema_val:
            sl_distance = min(atr_val * 1.5, self.p.max_sl_pips * pip_size)
            sl = price + sl_distance
            tp = price - (sl_distance * self.p.risk_reward)
            self.traded_today[name] = True
            self._enter_trade(d, name, False, price, sl, tp, sl_distance)
