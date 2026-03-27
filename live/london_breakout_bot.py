"""London Breakout Live Bot — MT5 Python API.

Runs the optimized London Breakout strategy on a live/demo MT5 account.
Requires: pip install MetaTrader5

Usage:
  python london_breakout_bot.py                    # Run on all pairs
  python london_breakout_bot.py --pairs EURUSD     # Single pair
  python london_breakout_bot.py --dry-run           # Log signals without trading

IMPORTANT: Must run on Windows with MT5 terminal open and logged in.
"""

import MetaTrader5 as mt5
import time
import logging
import argparse
import urllib.request
import urllib.parse
import json
import os
import ssl
from datetime import datetime, timezone, timedelta

# ── Optimized Parameters (walk-forward validated, score 163.8) ───────
PARAMS = {
    "asian_start_utc": 0,
    "asian_end_utc": 7,
    "london_entry_start_utc": 7,
    "london_entry_end_utc": 9,
    "session_close_utc": 21,
    "min_range_pips": 30,
    "max_range_pips": 60,
    "max_sl_pips": 50,
    "risk_reward": 1.5,
    "risk_pct": 0.015,  # 1.5% per trade
    "max_daily_loss_pct": 0.045,  # 4.5% circuit breaker (FTMO = 5%)
    "day_filter": [0, 1, 2, 3, 4],  # Mon-Fri
    "magic": 100001,
}

DEFAULT_PAIRS = ["EURUSD", "GBPUSD", "GBPJPY", "EURJPY"]

# ── Telegram Notifications ───────────────────────────────────────────
# Set in .env file (same directory as this script):
#   TELEGRAM_BOT_TOKEN=7123456789:AAH...
#   TELEGRAM_CHAT_ID=123456789
#
# Setup:
# 1. Message @BotFather on Telegram, send /newbot, follow prompts
# 2. Message your bot, then visit:
#    https://api.telegram.org/bot<TOKEN>/getUpdates
#    to find your chat_id

def _load_env():
    """Load .env file from script directory."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip())

_load_env()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Pip sizes per symbol
PIP_SIZES = {
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "GBPJPY": 0.01,
    "EURJPY": 0.01,
    "USDCAD": 0.0001,
    "EURCHF": 0.0001,
    "AUDNZD": 0.0001,
    "EURGBP": 0.0001,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("london_breakout.log"),
    ],
)
log = logging.getLogger("LB")


def send_telegram(message):
    """Send a message via Telegram bot."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(url, data=data)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        urllib.request.urlopen(req, timeout=10, context=ctx)
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


class LondonBreakoutBot:
    def __init__(self, pairs=None, dry_run=False, params=None):
        self.pairs = pairs or DEFAULT_PAIRS
        self.dry_run = dry_run
        self.p = params or PARAMS
        self.magic = self.p["magic"]

        # Per-pair state
        self.asian_high = {}
        self.asian_low = {}
        self.traded_today = {}
        self.current_date = {}

        # Daily loss tracking
        self.day_start_balance = None
        self.day_start_date = None

        # Daily trade log for summary
        self.daily_trades = []  # list of {pair, direction, entry, sl, tp, lots, time}
        self.daily_summary_sent = False

        for pair in self.pairs:
            self.asian_high[pair] = 0.0
            self.asian_low[pair] = float("inf")
            self.traded_today[pair] = False
            self.current_date[pair] = None

    def connect(self):
        """Initialize connection to MT5 terminal."""
        if not mt5.initialize():
            log.error(f"MT5 initialize failed: {mt5.last_error()}")
            return False

        info = mt5.account_info()
        if info is None:
            log.error("Failed to get account info")
            mt5.shutdown()
            return False

        log.info(f"Connected to MT5: {info.server}")
        log.info(f"Account: {info.login} | Balance: ${info.balance:,.2f} | Leverage: 1:{info.leverage}")
        log.info(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE TRADING'}")
        log.info(f"Pairs: {', '.join(self.pairs)}")

        # Verify all symbols are available
        for pair in self.pairs:
            sym = mt5.symbol_info(pair)
            if sym is None:
                log.error(f"Symbol {pair} not found. Check broker symbol names.")
                mt5.shutdown()
                return False
            if not sym.visible:
                mt5.symbol_select(pair, True)

        self._update_day_start_balance()

        mode = "DRY RUN" if self.dry_run else "LIVE"
        send_telegram(
            f"<b>Bot Started</b>\n"
            f"Mode: {mode}\n"
            f"Account: {info.login} | {info.server}\n"
            f"Balance: ${info.balance:,.2f}\n"
            f"Pairs: {', '.join(self.pairs)}"
        )
        return True

    def run(self):
        """Main loop — runs until killed."""
        if not self.connect():
            return

        log.info("Bot started. Checking every 15 seconds...")
        try:
            while True:
                self._tick()
                time.sleep(15)
        except KeyboardInterrupt:
            log.info("Shutting down...")
        finally:
            mt5.shutdown()

    def _tick(self):
        """Process one tick cycle for all pairs."""
        now_utc = datetime.now(timezone.utc)
        hour = now_utc.hour
        today = now_utc.date()
        weekday = now_utc.weekday()  # 0=Mon

        # Skip weekends
        if weekday >= 5:
            return

        # Daily reset
        if self.day_start_date != today:
            self._reset_day(today)

        # Check daily loss circuit breaker
        if self._daily_loss_exceeded():
            return

        for pair in self.pairs:
            # Reset per-pair state on new day
            if self.current_date[pair] != today:
                self.asian_high[pair] = 0.0
                self.asian_low[pair] = float("inf")
                self.traded_today[pair] = False
                self.current_date[pair] = today

            # Day filter
            if weekday not in self.p["day_filter"]:
                continue

            # Phase 1: Build Asian range
            if self.p["asian_start_utc"] <= hour < self.p["asian_end_utc"]:
                self._update_asian_range(pair)

            # Phase 2: London entry window
            elif self.p["london_entry_start_utc"] <= hour < self.p["london_entry_end_utc"]:
                if not self.traded_today[pair] and not self._has_position(pair):
                    self._check_breakout(pair)

            # Phase 3: Session close
            elif hour >= self.p["session_close_utc"]:
                self._close_position(pair)

        # Send daily summary once after session close
        if hour >= self.p["session_close_utc"]:
            self._send_daily_summary()

    def _update_asian_range(self, pair):
        """Update Asian session high/low from recent bars."""
        rates = mt5.copy_rates_from_pos(pair, mt5.TIMEFRAME_M15, 0, 1)
        if rates is None or len(rates) == 0:
            return
        bar = rates[0]
        self.asian_high[pair] = max(self.asian_high[pair], bar["high"])
        self.asian_low[pair] = min(self.asian_low[pair], bar["low"])

    def _check_breakout(self, pair):
        """Check for breakout above/below Asian range."""
        asian_high = self.asian_high[pair]
        asian_low = self.asian_low[pair]

        if asian_high == 0.0 or asian_low == float("inf"):
            return

        pip_size = PIP_SIZES.get(pair, 0.0001)
        range_pips = (asian_high - asian_low) / pip_size

        if range_pips < self.p["min_range_pips"] or range_pips > self.p["max_range_pips"]:
            return

        tick = mt5.symbol_info_tick(pair)
        if tick is None:
            return

        # Breakout LONG
        if tick.ask > asian_high:
            sl = asian_low
            sl_distance = tick.ask - sl
            sl_pips = sl_distance / pip_size

            if sl_pips > self.p["max_sl_pips"]:
                sl = tick.ask - self.p["max_sl_pips"] * pip_size
                sl_distance = tick.ask - sl

            tp = tick.ask + sl_distance * self.p["risk_reward"]
            lots = self._calc_lot_size(pair, sl_distance)

            if lots > 0:
                self._place_trade(pair, "buy", tick.ask, sl, tp, lots)
                self.traded_today[pair] = True

        # Breakout SHORT
        elif tick.bid < asian_low:
            sl = asian_high
            sl_distance = sl - tick.bid

            sl_pips = sl_distance / pip_size
            if sl_pips > self.p["max_sl_pips"]:
                sl = tick.bid + self.p["max_sl_pips"] * pip_size
                sl_distance = sl - tick.bid

            tp = tick.bid - sl_distance * self.p["risk_reward"]
            lots = self._calc_lot_size(pair, sl_distance)

            if lots > 0:
                self._place_trade(pair, "sell", tick.bid, sl, tp, lots)
                self.traded_today[pair] = True

    def _calc_lot_size(self, pair, sl_distance):
        """Calculate lot size based on risk percentage."""
        account = mt5.account_info()
        if account is None:
            return 0.0

        risk_amount = account.balance * self.p["risk_pct"]

        sym = mt5.symbol_info(pair)
        if sym is None:
            return 0.0

        # tick_value = profit per tick per lot
        # tick_size = minimum price change
        tick_value = sym.trade_tick_value
        tick_size = sym.trade_tick_size

        if tick_value == 0 or tick_size == 0:
            return 0.0

        # Value of sl_distance per 1 lot
        sl_value_per_lot = (sl_distance / tick_size) * tick_value
        if sl_value_per_lot == 0:
            return 0.0

        lots = risk_amount / sl_value_per_lot

        # Clamp to broker limits
        lots = max(sym.volume_min, lots)
        lots = min(sym.volume_max, lots)

        # Round down to lot step
        step = sym.volume_step
        lots = int(lots / step) * step
        lots = round(lots, 2)

        return lots

    def _place_trade(self, pair, direction, price, sl, tp, lots):
        """Place a market order."""
        sym = mt5.symbol_info(pair)
        digits = sym.digits

        sl = round(sl, digits)
        tp = round(tp, digits)
        price = round(price, digits)

        pip_size = PIP_SIZES.get(pair, 0.0001)
        sl_pips = abs(price - sl) / pip_size
        tp_pips = abs(tp - price) / pip_size

        log.info(
            f"{'[DRY RUN] ' if self.dry_run else ''}"
            f"SIGNAL: {direction.upper()} {pair} | {lots} lots | "
            f"Entry: {price} | SL: {sl} ({sl_pips:.0f} pips) | "
            f"TP: {tp} ({tp_pips:.0f} pips)"
        )

        if self.dry_run:
            return True

        order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pair,
            "volume": lots,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 10,
            "magic": self.magic,
            "comment": "LB_v1",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None:
            log.error(f"Order send failed: {mt5.last_error()}")
            return False

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            log.error(f"Order rejected: retcode={result.retcode} comment={result.comment}")
            return False

        log.info(f"Order filled: ticket={result.order} | {direction} {lots} {pair} @ {result.price}")
        send_telegram(
            f"<b>TRADE</b> {direction.upper()} {pair}\n"
            f"Lots: {lots} | Entry: {result.price}\n"
            f"SL: {sl} ({sl_pips:.0f} pips) | TP: {tp} ({tp_pips:.0f} pips)"
        )
        return True

    def _has_position(self, pair):
        """Check if we already have an open position on this pair."""
        positions = mt5.positions_get(symbol=pair)
        if positions is None:
            return False
        return any(p.magic == self.magic for p in positions)

    def _close_position(self, pair):
        """Close any open position on this pair."""
        positions = mt5.positions_get(symbol=pair)
        if positions is None:
            return

        for pos in positions:
            if pos.magic != self.magic:
                continue

            close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
            tick = mt5.symbol_info_tick(pair)
            close_price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask

            log.info(
                f"{'[DRY RUN] ' if self.dry_run else ''}"
                f"SESSION CLOSE: closing {pair} ticket={pos.ticket} P/L=${pos.profit:+.2f}"
            )

            if self.dry_run:
                continue

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": pair,
                "volume": pos.volume,
                "type": close_type,
                "position": pos.ticket,
                "price": close_price,
                "deviation": 10,
                "magic": self.magic,
                "comment": "LB_close",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                log.info(f"Position closed: ticket={pos.ticket}")
            else:
                log.error(f"Close failed: {result.retcode if result else mt5.last_error()}")

    def _daily_loss_exceeded(self):
        """Check if daily loss exceeds circuit breaker."""
        if self.day_start_balance is None:
            return False

        account = mt5.account_info()
        if account is None:
            return False

        daily_loss = self.day_start_balance - account.equity
        daily_loss_pct = daily_loss / self.day_start_balance

        if daily_loss_pct >= self.p["max_daily_loss_pct"]:
            log.warning(
                f"CIRCUIT BREAKER: daily loss {daily_loss_pct*100:.1f}% "
                f"(${daily_loss:,.0f}) — closing all positions, no more trades today"
            )
            send_telegram(
                f"<b>CIRCUIT BREAKER</b>\n"
                f"Daily loss: {daily_loss_pct*100:.1f}% (${daily_loss:,.0f})\n"
                f"Closing all positions. No more trades today."
            )
            for pair in self.pairs:
                self._close_position(pair)
                self.traded_today[pair] = True  # prevent new trades
            return True

        return False

    def _reset_day(self, today):
        """Reset daily state."""
        self.day_start_date = today
        self._update_day_start_balance()
        self.daily_trades = []
        self.daily_summary_sent = False
        for pair in self.pairs:
            self.asian_high[pair] = 0.0
            self.asian_low[pair] = float("inf")
            self.traded_today[pair] = False
            self.current_date[pair] = today
        log.info(f"New day: {today} | Start balance: ${self.day_start_balance:,.2f}")

    def _update_day_start_balance(self):
        """Record start-of-day balance for FTMO daily loss tracking."""
        account = mt5.account_info()
        if account:
            # FTMO uses max(balance, equity) at day start
            self.day_start_balance = max(account.balance, account.equity)

    def _send_daily_summary(self):
        """Send end-of-day Telegram report."""
        if self.daily_summary_sent:
            return
        self.daily_summary_sent = True

        account = mt5.account_info()
        if account is None:
            return

        balance = account.balance
        equity = account.equity

        # Get today's closed deals from MT5 history
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = now

        # Pull deal history for today
        deals = mt5.history_deals_get(day_start, day_end)
        my_deals = []
        if deals:
            my_deals = [d for d in deals if d.magic == self.magic and d.entry == mt5.DEAL_ENTRY_OUT]

        total_trades = len(my_deals)
        wins = sum(1 for d in my_deals if d.profit > 0)
        losses = sum(1 for d in my_deals if d.profit < 0)
        breakeven = total_trades - wins - losses
        day_pnl = sum(d.profit + d.commission + d.swap for d in my_deals)
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

        # Per-pair breakdown
        pair_pnl = {}
        for d in my_deals:
            pair_pnl[d.symbol] = pair_pnl.get(d.symbol, 0) + d.profit + d.commission + d.swap

        # Overall P/L since start (from initial balance if tracked)
        daily_dd = 0
        if self.day_start_balance and self.day_start_balance > 0:
            daily_dd = (self.day_start_balance - equity) / self.day_start_balance * 100

        # Streak info
        results = ["W" if d.profit > 0 else "L" for d in my_deals if d.profit != 0]
        streak = ""
        if results:
            last = results[-1]
            count = 0
            for r in reversed(results):
                if r == last:
                    count += 1
                else:
                    break
            streak = f"{'Win' if last == 'W' else 'Loss'} streak: {count}"

        # Build message
        mode = "DRY RUN" if self.dry_run else "LIVE"
        pnl_emoji = "+" if day_pnl >= 0 else ""

        msg = f"<b>London Breakout — Daily Report</b>\n"
        msg += f"<code>{now.strftime('%Y-%m-%d')} | {mode}</code>\n\n"

        msg += f"<b>Trades:</b> {total_trades}"
        if total_trades > 0:
            msg += f" ({wins}W / {losses}L / {breakeven}BE)\n"
            msg += f"<b>Win Rate:</b> {win_rate:.0f}%\n"
        else:
            msg += " (no trades)\n"

        msg += f"\n<b>Day P/L:</b> <code>{pnl_emoji}${day_pnl:,.2f}</code>\n"
        msg += f"<b>Daily DD:</b> {daily_dd:.1f}%\n"

        if pair_pnl:
            msg += f"\n<b>By Pair:</b>\n"
            for pair, pnl in sorted(pair_pnl.items(), key=lambda x: x[1], reverse=True):
                p_emoji = "+" if pnl >= 0 else ""
                msg += f"  {pair}: <code>{p_emoji}${pnl:,.2f}</code>\n"

        msg += f"\n<b>Balance:</b> ${balance:,.2f}\n"
        msg += f"<b>Equity:</b> ${equity:,.2f}\n"

        if streak:
            msg += f"\n{streak}"

        # FTMO progress (assuming $120K account, 10% target)
        initial_balance = 120000
        total_return_pct = (balance - initial_balance) / initial_balance * 100
        msg += f"\n\n<b>FTMO Progress:</b> {total_return_pct:+.1f}% / 10.0% target"

        send_telegram(msg)
        log.info(f"Daily summary sent: {total_trades} trades, P/L ${day_pnl:+,.2f}")


def main():
    parser = argparse.ArgumentParser(description="London Breakout MT5 Bot")
    parser.add_argument("--pairs", nargs="+", default=DEFAULT_PAIRS, help="Pairs to trade")
    parser.add_argument("--dry-run", action="store_true", help="Log signals without placing trades")
    parser.add_argument("--risk", type=float, default=None, help="Override risk_pct (e.g. 0.015)")
    args = parser.parse_args()

    params = dict(PARAMS)
    if args.risk is not None:
        params["risk_pct"] = args.risk

    bot = LondonBreakoutBot(pairs=args.pairs, dry_run=args.dry_run, params=params)
    bot.run()


if __name__ == "__main__":
    main()
