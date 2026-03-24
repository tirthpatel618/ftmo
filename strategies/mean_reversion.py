"""Mean Reversion Strategy for ranging pairs.

Trades Bollinger Band bounces on structurally range-bound pairs.
- Pairs: EUR/CHF, AUD/NZD, EUR/GBP
- Buy at lower band + RSI oversold
- Sell at upper band + RSI overbought
- TP at midline, SL beyond band + ATR buffer
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
    )

    def __init__(self):
        self._init_ftmo()

        self.bb = {}
        self.rsi = {}
        self.atr = {}
        self.orders = {}
        self.stop_orders = {}
        self.tp_orders = {}

        for d in self.datas:
            name = d._name
            self.bb[name] = bt.indicators.BollingerBands(
                d.close, period=self.p.bb_period, devfactor=self.p.bb_std
            )
            self.rsi[name] = bt.indicators.RSI(d.close, period=self.p.rsi_period)
            self.atr[name] = bt.indicators.ATR(d, period=self.p.atr_period)
            self.orders[name] = None
            self.stop_orders[name] = None
            self.tp_orders[name] = None

    def next(self):
        if not self._check_ftmo_limits():
            self._close_all_positions()
            return
        for d in self.datas:
            self._process_bar(d)

    def _process_bar(self, d):
        name = d._name
        dt = d.datetime.datetime(0)

        if dt.hour < 1 or dt.hour >= self.p.session_close:
            return

        if self.getposition(d).size != 0:
            return
        if self.orders.get(name):
            return

        price = d.close[0]
        bb = self.bb[name]
        rsi = self.rsi[name]
        atr_val = self.atr[name][0]

        if atr_val <= 0:
            return

        # BUY signal: price at or below lower band AND RSI oversold
        if price <= bb.lines.bot[0] and rsi[0] < self.p.rsi_oversold:
            sl = price - (atr_val * self.p.atr_sl_mult)
            tp = bb.lines.mid[0]
            sl_distance = price - sl

            if sl_distance > 0 and tp > price:
                self._enter_trade(d, name, True, price, sl, tp, sl_distance)

        # SELL signal: price at or above upper band AND RSI overbought
        elif price >= bb.lines.top[0] and rsi[0] > self.p.rsi_overbought:
            sl = price + (atr_val * self.p.atr_sl_mult)
            tp = bb.lines.mid[0]
            sl_distance = sl - price

            if sl_distance > 0 and tp < price:
                self._enter_trade(d, name, False, price, sl, tp, sl_distance)

    def _enter_trade(self, d, name, is_long, price, sl, tp, sl_distance):
        account_value = self.broker.getvalue()
        risk_amount = account_value * self.p.risk_pct
        size = risk_amount / sl_distance

        if size <= 0:
            return

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
