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
        ("squeeze_min_bars", 6),       # min bars in squeeze before trading
        ("risk_reward", 2.0),
        ("risk_pct", config.RISK_PER_TRADE_PCT),
        ("max_sl_pips", 40),
        ("session_start", 7),           # London open
        ("session_end", 20),            # NY close
        ("atr_period", 14),
    )

    def __init__(self):
        self._init_ftmo()
        self.bb = {}
        self.kc_upper = {}
        self.kc_lower = {}
        self.atr = {}
        self.squeeze_count = {}
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
            self.kc_upper[name] = ema + atr * self.p.kc_atr_mult
            self.kc_lower[name] = ema - atr * self.p.kc_atr_mult
            self.squeeze_count[name] = 0
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
            return  # Don't trade during squeeze, wait for release

        squeeze_bars = self.squeeze_count[name]
        self.squeeze_count[name] = 0  # Reset on release

        # Only trade during active sessions
        if hour < self.p.session_start or hour >= self.p.session_end:
            return

        # Need minimum squeeze duration
        if squeeze_bars < self.p.squeeze_min_bars:
            return

        if self.traded_today[name]:
            return
        if not self._can_trade(d):
            return

        price = d.close[0]

        # Breakout above upper BB = long
        if price > bb_up:
            sl_distance = min(atr_val * 1.5, self.p.max_sl_pips * pip_size)
            sl = price - sl_distance
            tp = price + (sl_distance * self.p.risk_reward)
            self.traded_today[name] = True
            self._enter_trade(d, name, True, price, sl, tp, sl_distance)

        # Breakout below lower BB = short
        elif price < bb_lo:
            sl_distance = min(atr_val * 1.5, self.p.max_sl_pips * pip_size)
            sl = price + sl_distance
            tp = price - (sl_distance * self.p.risk_reward)
            self.traded_today[name] = True
            self._enter_trade(d, name, False, price, sl, tp, sl_distance)
