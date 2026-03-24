"""Configuration for FTMO backtester and trading bot."""

# ── FTMO Challenge Rules ─────────────────────────────────────
INITIAL_BALANCE = 100_000
PROFIT_TARGET_PCT = 0.10       # 10% for Phase 1 challenge
VERIFICATION_TARGET_PCT = 0.05  # 5% for Phase 2
MAX_DAILY_LOSS_PCT = 0.05      # 5% daily loss limit
MAX_TOTAL_DRAWDOWN_PCT = 0.10  # 10% max drawdown from initial
MIN_TRADING_DAYS = 4
CHALLENGE_DAYS = 30
VERIFICATION_DAYS = 60

# ── Risk Management ──────────────────────────────────────────
RISK_PER_TRADE_PCT = 0.01  # 1% of account per trade
MAX_OPEN_TRADES = 3
MAX_DAILY_TRADES = 4

# ── Pairs ────────────────────────────────────────────────────
LONDON_BREAKOUT_PAIRS = ["EURUSD", "GBPUSD", "GBPJPY", "EURJPY"]
MEAN_REVERSION_PAIRS = ["EURCHF", "AUDNZD", "EURGBP"]
NY_MOMENTUM_PAIRS = ["EURUSD", "GBPUSD", "USDCAD"]
ALL_PAIRS = list(set(LONDON_BREAKOUT_PAIRS + MEAN_REVERSION_PAIRS + NY_MOMENTUM_PAIRS))

# ── Session Times (UTC hours) ────────────────────────────────
ASIAN_SESSION_START = 0   # 00:00 UTC
ASIAN_SESSION_END = 7     # 07:00 UTC
LONDON_OPEN = 7           # 07:00 UTC
LONDON_ENTRY_END = 11     # 11:00 UTC (last entry time, widened from 10)
NY_OPEN = 12              # 12:00 UTC
NY_ENTRY_END = 20         # 20:00 UTC (NY runs till close, was 16)
SESSION_CLOSE = 21        # 21:00 UTC (close all intraday trades, was 16)

# ── London Breakout Parameters ───────────────────────────────
LB_MIN_RANGE_PIPS = 20    # min Asian range to trade
LB_MAX_RANGE_PIPS = 80    # max Asian range to trade
LB_MAX_SL_PIPS = 50       # cap stop loss at 50 pips
LB_RISK_REWARD = 2.0      # take profit at 2x risk (higher R:R = more room to be wrong)
LB_USE_TREND_FILTER = False  # disabled — 200 EMA on 15min is too noisy
LB_DAY_FILTER = [0, 1, 2, 3, 4]  # Mon-Fri (was Tue-Thu only, too restrictive)

# ── Mean Reversion Parameters ────────────────────────────────
MR_BB_PERIOD = 20
MR_BB_STD = 1.8           # tighter bands = more signals (was 2.0)
MR_RSI_PERIOD = 14
MR_RSI_OVERSOLD = 38      # loosened from 30 — 30 + below BB is too rare on 15min
MR_RSI_OVERBOUGHT = 62    # loosened from 70
MR_ATR_SL_MULTIPLIER = 1.5  # SL = ATR * this beyond the band

# ── NY Momentum Parameters ───────────────────────────────────
NY_RSI_PERIOD = 14
NY_RSI_LONG_THRESHOLD = 55   # loosened from 60
NY_RSI_SHORT_THRESHOLD = 45  # loosened from 40
NY_RISK_REWARD = 2.0

# ── Backtesting ──────────────────────────────────────────────
COMMISSION_PIPS = 1.0      # FTMO provides tight spreads (~1 pip for majors)
DATA_DIR = "data/raw"
BACKTEST_START = "2024-01-01"
BACKTEST_END = "2025-12-31"
TIMEFRAME_MINUTES = 15     # primary timeframe for strategies

# ── Pip values per pair (USD per pip per standard lot) ───────
PIP_VALUES = {
    "EURUSD": 10.0,
    "GBPUSD": 10.0,
    "USDCAD": 10.0 / 1.36,  # approximate, varies with USD/CAD rate
    "GBPJPY": 10.0 / 150.0 * 100,  # approximate
    "EURJPY": 10.0 / 160.0 * 100,
    "EURCHF": 10.0 / 0.95,
    "AUDNZD": 10.0 / 1.70,
    "EURGBP": 10.0 / 0.85,
    "XAUUSD": 10.0,  # gold: 1 pip = $0.01, 1 lot = 100 oz
}

# Pip size (price movement that = 1 pip)
PIP_SIZES = {
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "USDCAD": 0.0001,
    "GBPJPY": 0.01,
    "EURJPY": 0.01,
    "EURCHF": 0.0001,
    "AUDNZD": 0.0001,
    "EURGBP": 0.0001,
    "XAUUSD": 0.01,
}
