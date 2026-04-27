"""LB Edge Analysis — replays the live bot's logic on 2 years of data
and breaks down win rate by feature buckets to find filterable edges.

Output: win rate, expectancy, R-multiple per bucket. Pairs with sample size
so we don't chase noise.
"""
import csv
import os
from datetime import datetime, timezone
from collections import defaultdict
from statistics import mean, stdev

# Live bot params (mirror the production config exactly)
PARAMS = {
    "asian_start": 0, "asian_end": 7,
    "london_start": 7, "london_end": 9,
    "session_close": 21,
    "min_range_pips": 30, "max_range_pips": 60,
    "max_sl_pips": 50,
    "risk_reward": 1.5,
    "day_filter": [0, 1, 2, 3, 4],
}
PAIRS = ["EURUSD", "GBPUSD", "GBPJPY", "EURJPY"]
PIP_SIZES = {"EURUSD": 0.0001, "GBPUSD": 0.0001, "GBPJPY": 0.01, "EURJPY": 0.01}
DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "raw")

def load_bars(pair):
    path = os.path.join(DATA_DIR, f"{pair}_15m.csv")
    bars = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            dt = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            bars.append({
                "dt": dt, "o": float(row["open"]), "h": float(row["high"]),
                "l": float(row["low"]), "c": float(row["close"]),
            })
    return bars

def atr(bars, idx, period=14):
    if idx < period:
        return 0
    trs = []
    for i in range(idx-period, idx):
        prev_close = bars[i-1]["c"] if i > 0 else bars[i]["c"]
        tr = max(
            bars[i]["h"] - bars[i]["l"],
            abs(bars[i]["h"] - prev_close),
            abs(bars[i]["l"] - prev_close),
        )
        trs.append(tr)
    return mean(trs) if trs else 0

def ema(bars, idx, period):
    if idx < period:
        return bars[idx]["c"]
    k = 2 / (period + 1)
    e = bars[idx-period]["c"]
    for i in range(idx-period+1, idx+1):
        e = bars[i]["c"] * k + e * (1 - k)
    return e

def simulate(pair):
    bars = load_bars(pair)
    pip_size = PIP_SIZES[pair]
    trades = []

    # Group bars by date
    by_date = defaultdict(list)
    for i, b in enumerate(bars):
        by_date[b["dt"].date()].append(i)

    dates = sorted(by_date.keys())

    for date in dates:
        idxs = by_date[date]
        if not idxs:
            continue

        weekday = date.weekday()
        if weekday not in PARAMS["day_filter"]:
            continue

        # Build Asian range
        asian_high, asian_low = 0.0, float("inf")
        london_idxs = []
        for i in idxs:
            h = bars[i]["dt"].hour
            if PARAMS["asian_start"] <= h < PARAMS["asian_end"]:
                asian_high = max(asian_high, bars[i]["h"])
                asian_low = min(asian_low, bars[i]["l"])
            elif PARAMS["london_start"] <= h < PARAMS["london_end"]:
                london_idxs.append(i)

        if asian_high == 0 or asian_low == float("inf"):
            continue

        range_pips = (asian_high - asian_low) / pip_size
        if range_pips < PARAMS["min_range_pips"] or range_pips > PARAMS["max_range_pips"]:
            continue

        # Find first breakout in London window
        traded = False
        for i in london_idxs:
            if traded:
                break
            price = bars[i]["c"]

            if bars[i]["h"] > asian_high and not traded:
                # LONG breakout (entry at asian_high; assume entry at break price)
                entry = asian_high
                sl = asian_low
                sl_dist = entry - sl
                sl_pips = sl_dist / pip_size
                if sl_pips > PARAMS["max_sl_pips"]:
                    sl = entry - PARAMS["max_sl_pips"] * pip_size
                    sl_dist = entry - sl
                tp = entry + sl_dist * PARAMS["risk_reward"]

                # Features at entry
                features = {
                    "pair": pair,
                    "date": date,
                    "weekday": weekday,
                    "direction": "long",
                    "range_pips": range_pips,
                    "sl_pips": sl_dist / pip_size,
                    "atr": atr(bars, i, 14),
                    "atr_pips": atr(bars, i, 14) / pip_size,
                    "ema200": ema(bars, i, 200),
                    "above_ema200": price > ema(bars, i, 200),
                    "asian_close_pos": (bars[idxs[asian_close_idx_for(idxs, bars)]]["c"] - asian_low) / (asian_high - asian_low) if asian_high > asian_low else 0.5,
                    "entry_hour": bars[i]["dt"].hour,
                }

                # Walk forward to determine outcome
                outcome = None
                for j in range(i, min(i + 80, len(bars))):  # max ~20 hours forward
                    if bars[j]["h"] >= tp:
                        outcome = ("WIN", PARAMS["risk_reward"])
                        break
                    if bars[j]["l"] <= sl:
                        outcome = ("LOSS", -1.0)
                        break
                    # Session close
                    if bars[j]["dt"].hour >= PARAMS["session_close"] and bars[j]["dt"].date() == date:
                        # Estimate exit at close
                        r = (bars[j]["c"] - entry) / sl_dist
                        outcome = ("CLOSE", r)
                        break

                if outcome:
                    features["result"] = outcome[0]
                    features["r_multiple"] = outcome[1]
                    trades.append(features)
                traded = True

            elif bars[i]["l"] < asian_low and not traded:
                entry = asian_low
                sl = asian_high
                sl_dist = sl - entry
                sl_pips = sl_dist / pip_size
                if sl_pips > PARAMS["max_sl_pips"]:
                    sl = entry + PARAMS["max_sl_pips"] * pip_size
                    sl_dist = sl - entry
                tp = entry - sl_dist * PARAMS["risk_reward"]

                features = {
                    "pair": pair,
                    "date": date,
                    "weekday": weekday,
                    "direction": "short",
                    "range_pips": range_pips,
                    "sl_pips": sl_dist / pip_size,
                    "atr": atr(bars, i, 14),
                    "atr_pips": atr(bars, i, 14) / pip_size,
                    "ema200": ema(bars, i, 200),
                    "above_ema200": price > ema(bars, i, 200),
                    "asian_close_pos": 0.5,
                    "entry_hour": bars[i]["dt"].hour,
                }

                outcome = None
                for j in range(i, min(i + 80, len(bars))):
                    if bars[j]["l"] <= tp:
                        outcome = ("WIN", PARAMS["risk_reward"])
                        break
                    if bars[j]["h"] >= sl:
                        outcome = ("LOSS", -1.0)
                        break
                    if bars[j]["dt"].hour >= PARAMS["session_close"] and bars[j]["dt"].date() == date:
                        r = (entry - bars[j]["c"]) / sl_dist
                        outcome = ("CLOSE", r)
                        break

                if outcome:
                    features["result"] = outcome[0]
                    features["r_multiple"] = outcome[1]
                    trades.append(features)
                traded = True

    return trades

def asian_close_idx_for(idxs, bars):
    # last bar before london open (07:00)
    for i in reversed(idxs):
        if bars[i]["dt"].hour < 7:
            return idxs.index(i)
    return 0

def bucket_stats(trades, key_fn, label):
    buckets = defaultdict(list)
    for t in trades:
        buckets[key_fn(t)].append(t)
    print(f"\n=== {label} ===")
    print(f"{'bucket':<25} {'n':>5} {'WR%':>6} {'avgR':>7} {'sumR':>7}")
    rows = []
    for k, ts in sorted(buckets.items()):
        n = len(ts)
        if n < 10:
            continue
        wins = sum(1 for t in ts if t["r_multiple"] > 0)
        wr = wins / n * 100
        avg_r = sum(t["r_multiple"] for t in ts) / n
        sum_r = sum(t["r_multiple"] for t in ts)
        rows.append((k, n, wr, avg_r, sum_r))
    for k, n, wr, avg_r, sum_r in rows:
        print(f"{str(k):<25} {n:>5} {wr:>5.1f}% {avg_r:>+6.2f}R {sum_r:>+6.1f}R")

if __name__ == "__main__":
    all_trades = []
    for pair in PAIRS:
        print(f"Loading {pair}...")
        ts = simulate(pair)
        print(f"  {len(ts)} trades generated")
        all_trades.extend(ts)

    print(f"\nTotal: {len(all_trades)} trades over 2 years\n")
    print("=" * 60)

    n = len(all_trades)
    wins = sum(1 for t in all_trades if t["r_multiple"] > 0)
    losses = sum(1 for t in all_trades if t["r_multiple"] < 0)
    avg_r = sum(t["r_multiple"] for t in all_trades) / n
    print(f"OVERALL: {n} trades, WR {wins/n*100:.1f}%, avg {avg_r:+.3f}R, total {sum(t['r_multiple'] for t in all_trades):+.1f}R")

    bucket_stats(all_trades, lambda t: t["pair"], "By Pair")
    bucket_stats(all_trades, lambda t: ["Mon","Tue","Wed","Thu","Fri"][t["weekday"]], "By Day")
    bucket_stats(all_trades, lambda t: t["direction"], "By Direction")
    bucket_stats(all_trades, lambda t: f"{int(t['range_pips']//10)*10}-{int(t['range_pips']//10)*10+10}", "By Range Size (pips)")
    bucket_stats(all_trades, lambda t: "above_ema200" if t["above_ema200"] else "below_ema200", "By Trend (200EMA on 15m)")
    bucket_stats(all_trades, lambda t: ("long_with_trend" if t["direction"]=="long" and t["above_ema200"]
                                  else "long_against_trend" if t["direction"]=="long"
                                  else "short_with_trend" if t["direction"]=="short" and not t["above_ema200"]
                                  else "short_against_trend"), "By Direction × Trend")
    bucket_stats(all_trades, lambda t: f"{int(t['atr_pips']//5)*5}+" if t['atr_pips']<30 else "30+", "By ATR (volatility, pips)")
    bucket_stats(all_trades, lambda t: f"hour_{t['entry_hour']}", "By Entry Hour")
