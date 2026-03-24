"""Mean Reversion Strategy for ranging pairs.

Trades Bollinger Band bounces on structurally range-bound pairs.
"""

import backtrader as bt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from strategies.ftmo_base import FTMOBase


class MeanReversion(FTMOBase):
    params = (
        ("bb_period", config.MR_BB_PERIOD),
        ("bb_std", config.MR_BB_STD),
        ("rsi_period", config.MR_RSI_PERIOD),
        ("rsi_oversold", config.MR_RSI_OVERSOLD),
        ("rsi_overbought", config.MR_RSI_OVERBOUGHT),
        ("atr_period", 14),
        ("atr_sl_mult", config.MR_ATR_SL_MULTIPLIER),
        ("risk_pct", config.RISK_PER_TRADE_PCT),
        ("session_close", config.SESSION_CLOSE),
        ("max_sl_pips", 30),  # cap SL to prevent huge positions on tight-range pairs
    )

    def __init__(self):
        self._init_ftmo()
        self.bb = {}
        self.rsi = {}
        self.atr = {}

        for d in self.datas:
            name = d._name
            self.bb[name] = bt.indicators.BollingerBands(
                d.close, period=self.p.bb_period, devfactor=self.p.bb_std
            )
            self.rsi[name] = bt.indicators.RSI(d.close, period=self.p.rsi_period)
            self.atr[name] = bt.indicators.ATR(d, period=self.p.atr_period)

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

        if dt.hour < 1 or dt.hour >= self.p.session_close:
            return

        if not self._can_trade(d):
            return

        price = d.close[0]
        bb = self.bb[name]
        rsi = self.rsi[name]
        atr_val = self.atr[name][0]
        pip_size = self._get_pip_size(name)

        if atr_val <= 0:
            return

        # BUY signal
        if price <= bb.lines.bot[0] and rsi[0] < self.p.rsi_oversold:
            sl_distance = atr_val * self.p.atr_sl_mult
            # Cap SL distance
            max_sl_distance = self.p.max_sl_pips * pip_size
            sl_distance = min(sl_distance, max_sl_distance)

            sl = price - sl_distance
            tp = bb.lines.mid[0]

            if sl_distance > 0 and tp > price:
                self._enter_trade(d, name, True, price, sl, tp, sl_distance)

        # SELL signal
        elif price >= bb.lines.top[0] and rsi[0] > self.p.rsi_overbought:
            sl_distance = atr_val * self.p.atr_sl_mult
            max_sl_distance = self.p.max_sl_pips * pip_size
            sl_distance = min(sl_distance, max_sl_distance)

            sl = price + sl_distance
            tp = bb.lines.mid[0]

            if sl_distance > 0 and tp < price:
                self._enter_trade(d, name, False, price, sl, tp, sl_distance)
