"""Pattern scanner — finds statistical edges in forex data.

Scans historical data for exploitable patterns:
- Session return distributions (which sessions trend vs range?)
- Day-of-week edge
- Hour-of-day profitability heatmap
- Volatility regime analysis
- Consecutive candle patterns
- Key level rejection stats (round numbers, daily H/L)
- Mean reversion after extreme moves

Run: python3 -m engine.pattern_scanner
"""

import pandas as pd
import numpy as np
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config


def load_pair(pair: str) -> pd.DataFrame:
    """Load 15min data for a pair."""
    path = Path(config.DATA_DIR) / f"{pair}_15m.csv"
    df = pd.read_csv(path, parse_dates=["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)
    df["hour"] = df["datetime"].dt.hour
    df["weekday"] = df["datetime"].dt.weekday  # 0=Mon
    df["date"] = df["datetime"].dt.date
    df["return"] = (df["close"] - df["open"]) / df["open"]
    df["range"] = (df["high"] - df["low"])
    df["body"] = abs(df["close"] - df["open"])
    df["bullish"] = (df["close"] > df["open"]).astype(int)
    return df


def session_returns(df: pd.DataFrame, pair: str):
    """Analyze returns by trading session."""
    sessions = {
        "Asian (0-7)": (0, 7),
        "London (7-12)": (7, 12),
        "NY Overlap (12-16)": (12, 16),
        "NY Afternoon (16-21)": (16, 21),
    }

    print(f"\n{'='*60}")
    print(f"SESSION RETURNS — {pair}")
    print(f"{'='*60}")

    for name, (start, end) in sessions.items():
        mask = (df["hour"] >= start) & (df["hour"] < end)
        session = df[mask]
        avg_ret = session["return"].mean() * 10000  # in pips
        std_ret = session["return"].std() * 10000
        avg_range = session["range"].mean()
        bullish_pct = session["bullish"].mean() * 100
        n = len(session)

        # Trend strength: how often does the session move >1 ATR in one direction?
        daily_sessions = session.groupby("date").agg(
            session_open=("open", "first"),
            session_close=("close", "last"),
            session_high=("high", "max"),
            session_low=("low", "min"),
        )
        daily_sessions["session_return"] = (
            daily_sessions["session_close"] - daily_sessions["session_open"]
        )
        daily_sessions["session_range"] = (
            daily_sessions["session_high"] - daily_sessions["session_low"]
        )
        # Efficiency: how much of the range was captured by the move
        daily_sessions["efficiency"] = abs(daily_sessions["session_return"]) / daily_sessions["session_range"].replace(0, np.nan)
        avg_efficiency = daily_sessions["efficiency"].mean()

        print(f"\n  {name}:")
        print(f"    Bars: {n} | Avg return: {avg_ret:+.2f} pips | Std: {std_ret:.2f} pips")
        print(f"    Bullish: {bullish_pct:.1f}% | Avg range: {avg_range:.5f}")
        print(f"    Trend efficiency: {avg_efficiency:.2f} (1.0 = pure trend, 0.0 = pure range)")


def day_of_week_edge(df: pd.DataFrame, pair: str):
    """Check for day-of-week effects."""
    days = ["Mon", "Tue", "Wed", "Thu", "Fri"]

    print(f"\n{'='*60}")
    print(f"DAY OF WEEK EDGE — {pair}")
    print(f"{'='*60}")

    daily = df.groupby("date").agg(
        open=("open", "first"),
        close=("close", "last"),
        high=("high", "max"),
        low=("low", "min"),
        weekday=("weekday", "first"),
    )
    daily["return_pips"] = (daily["close"] - daily["open"]) / config.PIP_SIZES.get(pair, 0.0001)
    daily["range_pips"] = (daily["high"] - daily["low"]) / config.PIP_SIZES.get(pair, 0.0001)

    for wd in range(5):
        day_data = daily[daily["weekday"] == wd]
        avg_ret = day_data["return_pips"].mean()
        avg_range = day_data["range_pips"].mean()
        bullish = (day_data["return_pips"] > 0).mean() * 100
        n = len(day_data)
        print(f"  {days[wd]}: avg {avg_ret:+.1f} pips | range {avg_range:.0f} pips | "
              f"bullish {bullish:.0f}% | n={n}")


def hour_heatmap(df: pd.DataFrame, pair: str):
    """Find best/worst hours for directional moves."""
    print(f"\n{'='*60}")
    print(f"HOUR-OF-DAY HEATMAP — {pair}")
    print(f"{'='*60}")

    pip_size = config.PIP_SIZES.get(pair, 0.0001)

    hourly = df.groupby("hour").agg(
        avg_return=("return", "mean"),
        std_return=("return", "std"),
        avg_range=("range", "mean"),
        bullish_pct=("bullish", "mean"),
        count=("return", "count"),
    )
    hourly["avg_return_pips"] = hourly["avg_return"] / pip_size
    hourly["avg_range_pips"] = hourly["avg_range"] / pip_size

    print(f"  {'Hour':>4} | {'Avg Ret':>8} | {'Range':>6} | {'Bull%':>5} | {'N':>5}")
    print(f"  {'-'*4}-+-{'-'*8}-+-{'-'*6}-+-{'-'*5}-+-{'-'*5}")

    for hour in range(24):
        if hour in hourly.index:
            row = hourly.loc[hour]
            ret = row["avg_return_pips"]
            rng = row["avg_range_pips"]
            bull = row["bullish_pct"] * 100
            n = int(row["count"])
            marker = " ***" if abs(ret) > 0.5 else ""
            print(f"  {hour:4d} | {ret:+8.2f} | {rng:6.1f} | {bull:5.1f} | {n:5d}{marker}")


def consecutive_candles(df: pd.DataFrame, pair: str):
    """After N consecutive bullish/bearish candles, what happens next?"""
    print(f"\n{'='*60}")
    print(f"CONSECUTIVE CANDLE PATTERNS — {pair}")
    print(f"{'='*60}")

    pip_size = config.PIP_SIZES.get(pair, 0.0001)

    for direction in ["bullish", "bearish"]:
        is_dir = df["bullish"] if direction == "bullish" else (1 - df["bullish"])

        # Count consecutive runs
        groups = (is_dir != is_dir.shift()).cumsum()
        run_lengths = is_dir.groupby(groups).cumsum()

        print(f"\n  After N consecutive {direction} candles → next candle:")
        print(f"  {'N':>3} | {'Continue%':>9} | {'Reverse%':>9} | {'Avg Next':>10} | {'Count':>5}")

        for n in [2, 3, 4, 5, 6]:
            # Find bars where we just completed N consecutive candles
            mask = (run_lengths == n) & (run_lengths.shift(-1) != n + 1)
            # Shift to get the NEXT bar after the run
            indices = df.index[mask]
            next_indices = indices + 1
            next_indices = next_indices[next_indices < len(df)]

            if len(next_indices) < 10:
                continue

            next_bars = df.loc[next_indices]
            if direction == "bullish":
                continuation = next_bars["bullish"].mean() * 100
                reversal = 100 - continuation
            else:
                continuation = (1 - next_bars["bullish"]).mean() * 100
                reversal = 100 - continuation

            avg_next_pips = next_bars["return"].mean() / pip_size
            if direction == "bearish":
                avg_next_pips = -avg_next_pips  # flip so positive = continuation

            count = len(next_indices)
            edge = "EDGE" if continuation > 55 or reversal > 55 else ""
            print(f"  {n:3d} | {continuation:8.1f}% | {reversal:8.1f}% | {avg_next_pips:+9.2f}p | {count:5d} {edge}")


def extreme_move_reversion(df: pd.DataFrame, pair: str):
    """After extreme moves (>2σ), does price revert?"""
    print(f"\n{'='*60}")
    print(f"EXTREME MOVE REVERSION — {pair}")
    print(f"{'='*60}")

    pip_size = config.PIP_SIZES.get(pair, 0.0001)

    # Rolling stats
    df = df.copy()
    df["ret_zscore"] = (df["return"] - df["return"].rolling(100).mean()) / df["return"].rolling(100).std()

    for threshold in [1.5, 2.0, 2.5, 3.0]:
        # Big up moves
        big_up = df[df["ret_zscore"] > threshold].index
        big_up_next = big_up + 1
        big_up_next = big_up_next[big_up_next < len(df)]

        # Big down moves
        big_down = df[df["ret_zscore"] < -threshold].index
        big_down_next = big_down + 1
        big_down_next = big_down_next[big_down_next < len(df)]

        if len(big_up_next) < 10 or len(big_down_next) < 10:
            continue

        # After big up: does next bar go down (revert)?
        after_up = df.loc[big_up_next, "return"]
        up_revert_pct = (after_up < 0).mean() * 100
        up_avg_next = after_up.mean() / pip_size

        # After big down: does next bar go up (revert)?
        after_down = df.loc[big_down_next, "return"]
        down_revert_pct = (after_down > 0).mean() * 100
        down_avg_next = after_down.mean() / pip_size

        # Multi-bar reversion (next 4 bars = 1 hour)
        up_4bar = []
        for idx in big_up:
            if idx + 4 < len(df):
                ret_4 = (df.loc[idx + 4, "close"] - df.loc[idx, "close"]) / pip_size
                up_4bar.append(ret_4)

        down_4bar = []
        for idx in big_down:
            if idx + 4 < len(df):
                ret_4 = (df.loc[idx + 4, "close"] - df.loc[idx, "close"]) / pip_size
                down_4bar.append(ret_4)

        up_4bar_avg = np.mean(up_4bar) if up_4bar else 0
        down_4bar_avg = np.mean(down_4bar) if down_4bar else 0

        edge_up = " ← EDGE" if up_revert_pct > 55 else ""
        edge_down = " ← EDGE" if down_revert_pct > 55 else ""

        print(f"\n  |z| > {threshold}:")
        print(f"    After big UP  (n={len(big_up_next):4d}): revert {up_revert_pct:.1f}% | "
              f"next bar {up_avg_next:+.2f}p | next 1hr {up_4bar_avg:+.2f}p{edge_up}")
        print(f"    After big DOWN(n={len(big_down_next):4d}): revert {down_revert_pct:.1f}% | "
              f"next bar {down_avg_next:+.2f}p | next 1hr {down_4bar_avg:+.2f}p{edge_down}")


def round_number_rejection(df: pd.DataFrame, pair: str):
    """Do prices bounce off round numbers (psychological levels)?"""
    print(f"\n{'='*60}")
    print(f"ROUND NUMBER REJECTION — {pair}")
    print(f"{'='*60}")

    pip_size = config.PIP_SIZES.get(pair, 0.0001)

    # Determine round number interval based on pair
    if pip_size == 0.01:  # JPY pairs
        round_interval = 0.50  # every 50 pips
    else:
        round_interval = 0.0050  # every 50 pips

    df = df.copy()
    # Distance to nearest round number
    df["nearest_round"] = (df["close"] / round_interval).round() * round_interval
    df["dist_to_round"] = abs(df["close"] - df["nearest_round"]) / pip_size

    # Bars near round numbers (within 5 pips)
    near = df[df["dist_to_round"] <= 5]
    far = df[df["dist_to_round"] > 20]

    if len(near) < 50 or len(far) < 50:
        print("  Not enough data near round numbers")
        return

    near_range = near["range"].mean() / pip_size
    far_range = far["range"].mean() / pip_size
    near_bull = near["bullish"].mean() * 100
    far_bull = far["bullish"].mean() * 100

    # Next bar after touching round number
    near_indices = near.index
    next_indices = near_indices + 1
    next_indices = next_indices[next_indices < len(df)]

    # Was price approaching from below or above?
    approaching_from_below = near[near["close"] > near["nearest_round"]]
    approaching_from_above = near[near["close"] < near["nearest_round"]]

    print(f"  Bars near round numbers (within 5 pips): {len(near)}")
    print(f"  Bars far from round numbers (>20 pips): {len(far)}")
    print(f"  Near: avg range {near_range:.1f}p, bullish {near_bull:.1f}%")
    print(f"  Far:  avg range {far_range:.1f}p, bullish {far_bull:.1f}%")
    print(f"  Range difference: {near_range - far_range:+.1f} pips (positive = more volatile near rounds)")


def volatility_regime_analysis(df: pd.DataFrame, pair: str):
    """Classify volatility regimes and check which is best for trending."""
    print(f"\n{'='*60}")
    print(f"VOLATILITY REGIME ANALYSIS — {pair}")
    print(f"{'='*60}")

    pip_size = config.PIP_SIZES.get(pair, 0.0001)
    df = df.copy()

    # ATR proxy: rolling 14-bar average range
    df["atr"] = df["range"].rolling(14 * 4).mean()  # ~14 hours
    df = df.dropna(subset=["atr"])

    # Classify into terciles
    q33 = df["atr"].quantile(0.33)
    q66 = df["atr"].quantile(0.66)

    regimes = {
        "Low vol": df[df["atr"] <= q33],
        "Med vol": df[(df["atr"] > q33) & (df["atr"] <= q66)],
        "High vol": df[df["atr"] > q66],
    }

    for name, regime in regimes.items():
        avg_range = regime["range"].mean() / pip_size
        avg_body = regime["body"].mean() / pip_size
        body_ratio = regime["body"].mean() / regime["range"].replace(0, np.nan).mean()
        bull_pct = regime["bullish"].mean() * 100
        # Trend efficiency: body/range ratio (higher = more trending)
        print(f"  {name}: range {avg_range:.1f}p | body {avg_body:.1f}p | "
              f"body/range {body_ratio:.2f} | bullish {bull_pct:.1f}% | n={len(regime)}")

    # Best regime for breakout strategies
    print(f"\n  Insight: body/range ratio indicates trend strength.")
    print(f"  Higher body/range = candles closing near highs/lows = good for breakout.")
    print(f"  Lower body/range = more wicks = good for mean reversion.")


def scan_all():
    """Run all scans on all pairs."""
    pairs = config.ALL_PAIRS
    print(f"Scanning {len(pairs)} pairs: {', '.join(pairs)}\n")

    for pair in sorted(pairs):
        try:
            df = load_pair(pair)
            print(f"\n{'#'*70}")
            print(f"# {pair} — {len(df)} bars ({df['datetime'].min()} to {df['datetime'].max()})")
            print(f"{'#'*70}")

            session_returns(df, pair)
            day_of_week_edge(df, pair)
            hour_heatmap(df, pair)
            consecutive_candles(df, pair)
            extreme_move_reversion(df, pair)
            round_number_rejection(df, pair)
            volatility_regime_analysis(df, pair)

        except Exception as e:
            print(f"\nError scanning {pair}: {e}")


if __name__ == "__main__":
    scan_all()
