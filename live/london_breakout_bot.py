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

DEFAULT_PAIRS = ["EURUSD", "GBPUSD", "GBPJPY", "EURJPY", "USDCAD", "AUDUSD", "NZDUSD", "EURGBP"]

# ── Extreme Reversion Parameters ─────────────────────────────────────
ER_PARAMS = {
    "pairs": ["AUDNZD", "EURCHF"],
    "lookback": 40,            # rolling window for z-score
    "z_threshold": 2.0,        # raised from 1.5 — fewer but higher conviction trades
    "risk_reward": 2.0,
    "risk_pct": 0.01,          # 1% per trade (lowered — these are counter-trend)
    "max_sl_pips": 30,
    "min_sl_pips": 15,         # NEW: floor to prevent tiny SL / massive lots
    "sl_buffer_atr_mult": 0.5, # SL = extreme wick + 0.5 ATR
    "atr_period": 14,
    "session_start_utc": 3,    # include late Asian for range pairs
    "session_end_utc": 20,
    "cooldown_minutes": 60,    # FIXED: time-based, not tick-based
    "max_er_trades_per_day": 2, # NEW: cap daily ER trades per pair
    "magic": 100002,
}

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
    "AUDUSD": 0.0001,
    "NZDUSD": 0.0001,
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

        # Per-pair state (London Breakout)
        self.asian_high = {}
        self.asian_low = {}
        self.traded_today = {}
        self.current_date = {}

        # Daily loss tracking
        self.day_start_balance = None
        self.day_start_date = None
        self.circuit_breaker_hit = False
        self.session_closed = False

        # Daily trade log for summary
        self.daily_trades = []
        self.daily_summary_sent = False

        # Track open positions to detect SL/TP closes
        self.tracked_positions = {}  # ticket -> {pair, direction, entry, sl, tp, lots, open_time}

        for pair in self.pairs:
            self.asian_high[pair] = 0.0
            self.asian_low[pair] = float("inf")
            self.traded_today[pair] = False
            self.current_date[pair] = None

        # ── Extreme Reversion state ──────────────────────────────────
        self.er = ER_PARAMS
        self.er_return_history = {}   # pair -> list of recent bar returns
        self.er_last_trade_time = {}  # pair -> datetime (cooldown)
        self.er_daily_trade_count = {} # pair -> int
        self.er_last_bar_time = {}    # pair -> datetime (avoid double-processing)
        for pair in self.er["pairs"]:
            self.er_return_history[pair] = []
            self.er_last_trade_time[pair] = None
            self.er_daily_trade_count[pair] = 0
            self.er_last_bar_time[pair] = None
            # Ensure symbol is visible in MT5
            if not dry_run:
                try:
                    sym = mt5.symbol_info(pair)
                    if sym and not sym.visible:
                        mt5.symbol_select(pair, True)
                except Exception:
                    pass

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

        # Verify all symbols are available (LB + ER pairs)
        all_pairs = list(set(self.pairs + self.er["pairs"]))
        for pair in all_pairs:
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
            f"LB Pairs: {', '.join(self.pairs)}\n"
            f"ER Pairs: {', '.join(self.er['pairs'])}"
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

        # Check if any open trades hit SL/TP
        self._check_closed_positions()

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

            # Phase 3: Session close — one attempt, then stop
            elif hour >= self.p["session_close_utc"]:
                if not self.session_closed and self._has_position(pair):
                    self._close_position(pair)

        # ── Extreme Reversion: disabled — AUDNZD/EURCHF trending, not ranging ──
        # ER was causing consistent losses ($3-6K/day) on trending AUDNZD.
        # LB alone is on track for FTMO target. Re-enable after challenge is passed.
        #
        # if self.er["session_start_utc"] <= hour < self.er["session_end_utc"]:
        #     if weekday in self.p["day_filter"]:
        #         for pair in self.er["pairs"]:
        #             self._check_extreme_reversion(pair)
        # elif hour >= self.p["session_close_utc"]:
        #     for pair in self.er["pairs"]:
        #         if self._has_er_position(pair):
        #             self._close_position(pair, magic=self.er["magic"])

        # Close any lingering ER positions from before this session
        if hour >= self.p["session_close_utc"]:
            for pair in self.er["pairs"]:
                self._close_position(pair, magic=self.er["magic"])

        # Mark session closed for logging purposes (no longer gates close logic)
        if hour >= self.p["session_close_utc"] and not self.session_closed:
            self.session_closed = True
            log.info("Session closed. No more trading until next day.")

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

    def _check_extreme_reversion(self, pair):
        """Check for extreme z-score move and fade it."""
        now = datetime.now(timezone.utc)

        # Time-based cooldown (not tick-based)
        last_trade = self.er_last_trade_time.get(pair)
        if last_trade:
            minutes_since = (now - last_trade).total_seconds() / 60
            if minutes_since < self.er["cooldown_minutes"]:
                return

        # Daily trade limit per pair
        if self.er_daily_trade_count.get(pair, 0) >= self.er["max_er_trades_per_day"]:
            return

        # Get completed bars
        rates = mt5.copy_rates_from_pos(pair, mt5.TIMEFRAME_M15, 1, self.er["lookback"] + 1)
        if rates is None or len(rates) < self.er["lookback"]:
            return

        # Only process once per new bar
        bar_time = datetime.fromtimestamp(rates[-1]["time"], tz=timezone.utc)
        if self.er_last_bar_time.get(pair) == bar_time:
            return
        self.er_last_bar_time[pair] = bar_time

        # Already have ER position on this pair?
        positions = mt5.positions_get(symbol=pair)
        if positions and any(p.magic == self.er["magic"] for p in positions):
            return

        # Calculate returns for all bars
        returns = []
        for i in range(1, len(rates)):
            if rates[i]["open"] == 0:
                returns.append(0)
            else:
                returns.append((rates[i]["close"] - rates[i]["open"]) / rates[i]["open"])

        if len(returns) < self.er["lookback"]:
            return

        # Z-score of the latest bar
        recent = returns[-self.er["lookback"]:]
        mean_ret = sum(recent) / len(recent)
        var = sum((r - mean_ret) ** 2 for r in recent) / len(recent)
        std_ret = var ** 0.5

        if std_ret == 0:
            return

        latest_return = returns[-1]
        z_score = (latest_return - mean_ret) / std_ret

        if abs(z_score) < self.er["z_threshold"]:
            return

        # Calculate ATR for SL buffer
        atr_sum = 0
        for i in range(-self.er["atr_period"], 0):
            atr_sum += rates[i]["high"] - rates[i]["low"]
        atr = atr_sum / self.er["atr_period"]

        pip_size = PIP_SIZES.get(pair, 0.0001)
        min_sl = self.er["min_sl_pips"] * pip_size
        last_bar = rates[-1]
        tick = mt5.symbol_info_tick(pair)
        if tick is None:
            return

        if z_score > self.er["z_threshold"]:
            # Extreme UP → SHORT (fade)
            price = tick.bid
            sl = last_bar["high"] + atr * self.er["sl_buffer_atr_mult"]
            sl_distance = sl - price

            # Enforce minimum SL distance
            if sl_distance < min_sl:
                sl = price + min_sl
                sl_distance = min_sl

            # Cap at max SL
            sl_pips = sl_distance / pip_size
            if sl_pips > self.er["max_sl_pips"]:
                sl = price + self.er["max_sl_pips"] * pip_size
                sl_distance = sl - price

            tp = price - sl_distance * self.er["risk_reward"]
            lots = self._calc_lot_size(pair, sl_distance, self.er["risk_pct"])

            if lots > 0:
                log.info(f"ER SIGNAL: z={z_score:.1f} | SELL {pair} | SL {sl_pips:.0f} pips")
                self._place_trade(pair, "sell", price, sl, tp, lots, magic=self.er["magic"], tag="ER")
                self.er_last_trade_time[pair] = now
                self.er_daily_trade_count[pair] = self.er_daily_trade_count.get(pair, 0) + 1

        elif z_score < -self.er["z_threshold"]:
            # Extreme DOWN → LONG (fade)
            price = tick.ask
            sl = last_bar["low"] - atr * self.er["sl_buffer_atr_mult"]
            sl_distance = price - sl

            # Enforce minimum SL distance
            if sl_distance < min_sl:
                sl = price - min_sl
                sl_distance = min_sl

            # Cap at max SL
            sl_pips = sl_distance / pip_size
            if sl_pips > self.er["max_sl_pips"]:
                sl = price - self.er["max_sl_pips"] * pip_size
                sl_distance = price - sl

            tp = price + sl_distance * self.er["risk_reward"]
            lots = self._calc_lot_size(pair, sl_distance, self.er["risk_pct"])

            if lots > 0:
                log.info(f"ER SIGNAL: z={z_score:.1f} | BUY {pair} | SL {sl_pips:.0f} pips")
                self._place_trade(pair, "buy", price, sl, tp, lots, magic=self.er["magic"], tag="ER")
                self.er_last_trade_time[pair] = now
                self.er_daily_trade_count[pair] = self.er_daily_trade_count.get(pair, 0) + 1

    def _check_breakout(self, pair):
        """Check for LONG breakout above Asian range, with 200 EMA trend filter.

        Edge analysis on 520 historical trades found:
          - Longs: 54.7% WR, +0.20R/trade
          - Shorts: 44.6% WR, +0.07R/trade (essentially breakeven)
          - Long + above 200EMA: 56.2% WR, +0.21R/trade
        Shorts are disabled. Longs require price above 200 EMA on 15m.
        """
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

        # Only LONG breakouts — shorts have no edge in 2 years of data
        if tick.ask <= asian_high:
            return

        # 200 EMA trend filter — skip if price is below 200 EMA
        ema200 = self._calc_ema200(pair)
        if ema200 is None or tick.ask < ema200:
            return

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

    def _calc_ema200(self, pair):
        """Compute 200-period EMA on 15m bars."""
        rates = mt5.copy_rates_from_pos(pair, mt5.TIMEFRAME_M15, 0, 250)
        if rates is None or len(rates) < 200:
            return None
        k = 2 / 201
        e = float(rates[0]["close"])
        for r in rates[1:]:
            e = float(r["close"]) * k + e * (1 - k)
        return e

    def _calc_lot_size(self, pair, sl_distance, risk_pct=None):
        """Calculate lot size based on risk percentage."""
        account = mt5.account_info()
        if account is None:
            return 0.0

        risk_amount = account.balance * (risk_pct or self.p["risk_pct"])

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

    def _place_trade(self, pair, direction, price, sl, tp, lots, magic=None, tag="LB"):
        """Place a market order."""
        use_magic = magic or self.magic
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
            f"[{tag}] SIGNAL: {direction.upper()} {pair} | {lots} lots | "
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
            "magic": use_magic,
            "comment": f"{tag}_v1",
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

        fill_price = result.price if result.price > 0 else price
        log.info(f"[{tag}] Order filled: ticket={result.order} | {direction} {lots} {pair} @ {fill_price}")
        send_telegram(
            f"<b>[{tag}] TRADE</b> {direction.upper()} {pair}\n"
            f"Lots: {lots} | Entry: {fill_price}\n"
            f"SL: {sl} ({sl_pips:.0f} pips) | TP: {tp} ({tp_pips:.0f} pips)"
        )

        # Track position for close detection
        self.tracked_positions[result.order] = {
            "pair": pair,
            "direction": direction,
            "entry": fill_price,
            "sl": sl,
            "tp": tp,
            "lots": lots,
            "open_time": datetime.now(timezone.utc),
            "tag": tag,
        }
        return True

    def _check_closed_positions(self):
        """Detect when tracked positions close (SL/TP hit) and alert."""
        if not self.tracked_positions:
            return

        # Get all currently open position tickets for our magics (LB + ER)
        our_magics = {self.magic, self.er["magic"]}
        open_tickets = set()
        positions = mt5.positions_get()
        if positions:
            for p in positions:
                if p.magic in our_magics:
                    open_tickets.add(p.ticket)

        # Check which tracked positions are no longer open
        closed_tickets = [t for t in self.tracked_positions if t not in open_tickets]

        for ticket in closed_tickets:
            info = self.tracked_positions.pop(ticket)
            pip_size = PIP_SIZES.get(info["pair"], 0.0001)

            # Look up the closed deal by position ID — most reliable method,
            # no timezone issues since we search by ID not timestamp
            now = datetime.now(timezone.utc)
            profit = 0.0
            close_price = 0.0
            close_reason = "Unknown"

            deals = mt5.history_deals_get(position=ticket)
            if deals:
                for d in deals:
                    if d.entry == mt5.DEAL_ENTRY_OUT:
                        profit = d.profit + d.commission + d.swap
                        close_price = d.price
                        if d.reason == 4:    # DEAL_REASON_SL
                            close_reason = "Stop Loss"
                        elif d.reason == 5:  # DEAL_REASON_TP
                            close_reason = "Take Profit"
                        elif d.reason == 6:  # DEAL_REASON_SO (stop out)
                            close_reason = "Stop Out"
                        elif d.reason in (0, 1, 2, 3):
                            close_reason = "Bot/Manual Close"
                        else:
                            close_reason = f"Code {d.reason}"
                        break

            # Fallback: current market price estimate
            if close_price == 0.0:
                tick = mt5.symbol_info_tick(info["pair"])
                if tick:
                    close_price = tick.bid if info["direction"] == "buy" else tick.ask

            # Calculate pips gained/lost
            if info["direction"] == "buy":
                pips_result = (close_price - info["entry"]) / pip_size if close_price > 0 else 0
            else:
                pips_result = (info["entry"] - close_price) / pip_size if close_price > 0 else 0

            duration = now - info["open_time"]
            hours = duration.total_seconds() / 3600

            result_label = "WIN" if profit > 0 else "LOSS" if profit < 0 else "BE"
            tag = info.get("tag", "LB")

            log.info(
                f"[{tag}] CLOSED: {info['direction'].upper()} {info['pair']} | "
                f"{close_reason} | P/L: ${profit:+,.2f} ({pips_result:+.0f} pips) | "
                f"Duration: {hours:.1f}h"
            )

            send_telegram(
                f"<b>[{tag}] CLOSED — {result_label}</b>\n"
                f"{info['direction'].upper()} {info['pair']} | {info['lots']} lots\n"
                f"Reason: {close_reason}\n"
                f"Entry: {info['entry']} → Exit: {close_price}\n"
                f"P/L: <code>${profit:+,.2f}</code> ({pips_result:+.0f} pips)\n"
                f"Duration: {hours:.1f}h"
            )

    def _has_position(self, pair):
        """Check if we already have an open position on this pair."""
        positions = mt5.positions_get(symbol=pair)
        if positions is None:
            return False
        return any(p.magic == self.magic for p in positions)

    def _close_position(self, pair, magic=None):
        """Close any open position on this pair."""
        use_magic = magic or self.magic
        positions = mt5.positions_get(symbol=pair)
        if positions is None:
            return

        for pos in positions:
            if pos.magic != use_magic:
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
                "magic": use_magic,
                "comment": "close",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                log.info(f"Position closed: ticket={pos.ticket} P/L=${pos.profit:+.2f}")
                # Send close alert immediately (don't wait for _check_closed_positions)
                if pos.ticket in self.tracked_positions:
                    info = self.tracked_positions.pop(pos.ticket)
                    pip_size = PIP_SIZES.get(pair, 0.0001)
                    tag = info.get("tag", "LB")
                    if info["direction"] == "buy":
                        pips = (close_price - info["entry"]) / pip_size
                    else:
                        pips = (info["entry"] - close_price) / pip_size
                    duration = (datetime.now(timezone.utc) - info["open_time"]).total_seconds() / 3600
                    result_label = "WIN" if pos.profit > 0 else "LOSS" if pos.profit < 0 else "BE"
                    send_telegram(
                        f"<b>[{tag}] CLOSED — {result_label}</b>\n"
                        f"{info['direction'].upper()} {pair} | {info['lots']} lots\n"
                        f"Reason: Session Close\n"
                        f"Entry: {info['entry']} → Exit: {close_price}\n"
                        f"P/L: <code>${pos.profit:+,.2f}</code> ({pips:+.0f} pips)\n"
                        f"Duration: {duration:.1f}h"
                    )
            elif result:
                retcode = result.retcode
                # 10018 = market closed, 10031 = no connection — don't spam
                if retcode in (10018, 10031):
                    log.warning(f"Cannot close {pair} ticket={pos.ticket}: market closed (code {retcode})")
                else:
                    log.error(f"Close failed: retcode={retcode} comment={result.comment}")

    def _daily_loss_exceeded(self):
        """Check if daily loss exceeds circuit breaker."""
        if self.day_start_balance is None:
            return False

        # Already tripped today — skip all checks silently
        if self.circuit_breaker_hit:
            return True

        account = mt5.account_info()
        if account is None:
            return False

        daily_loss = self.day_start_balance - account.equity
        daily_loss_pct = daily_loss / self.day_start_balance

        if daily_loss_pct >= self.p["max_daily_loss_pct"]:
            self.circuit_breaker_hit = True
            log.warning(
                f"CIRCUIT BREAKER: daily loss {daily_loss_pct*100:.1f}% "
                f"(${daily_loss:,.0f}) — closing all positions, no more trades today"
            )
            send_telegram(
                f"<b>CIRCUIT BREAKER</b>\n"
                f"Daily loss: {daily_loss_pct*100:.1f}% (${daily_loss:,.0f})\n"
                f"Closing all positions. No more trades today."
            )
            # Close all LB positions
            for pair in self.pairs:
                self._close_position(pair)
                self.traded_today[pair] = True
            # Close all ER positions
            for pair in self.er["pairs"]:
                self._close_position(pair, magic=self.er["magic"])
            return True

        return False

    def _reset_day(self, today):
        """Reset daily state."""
        self.day_start_date = today
        self._update_day_start_balance()
        self.daily_trades = []
        self.daily_summary_sent = False
        self.circuit_breaker_hit = False
        self.session_closed = False
        for pair in self.pairs:
            self.asian_high[pair] = 0.0
            self.asian_low[pair] = float("inf")
            self.traded_today[pair] = False
            self.current_date[pair] = today
        # Reset ER daily counters
        for pair in self.er["pairs"]:
            self.er_daily_trade_count[pair] = 0
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
            our_magics = {self.magic, self.er["magic"]}
            my_deals = [d for d in deals if d.magic in our_magics and d.entry == mt5.DEAL_ENTRY_OUT]

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
