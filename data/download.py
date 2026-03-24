"""Download historical forex data from Dukascopy with controlled concurrency."""

import struct
import lzma
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

DUKASCOPY_URL = "https://datafeed.dukascopy.com/datafeed"

POINT_VALUES = {
    "EURUSD": 1e5, "GBPUSD": 1e5, "GBPJPY": 1e3, "EURJPY": 1e3,
    "USDCAD": 1e5, "EURCHF": 1e5, "AUDNZD": 1e5, "EURGBP": 1e5,
    "XAUUSD": 1e3,
}


def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
    })
    return s


def download_hour(session, pair: str, dt: datetime, retries: int = 3) -> tuple[datetime, bytes | None]:
    """Download one hour of tick data with retries."""
    month_zero = dt.month - 1
    url = (
        f"{DUKASCOPY_URL}/{pair}/"
        f"{dt.year}/{month_zero:02d}/{dt.day:02d}/"
        f"{dt.hour:02d}h_ticks.bi5"
    )
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=15)
            if resp.status_code == 200 and len(resp.content) > 0:
                return dt, resp.content
            if resp.status_code == 404:
                return dt, None  # No data for this hour (weekend/holiday)
            # Rate limited or server error — wait and retry
            if resp.status_code in (429, 500, 502, 503):
                import time
                time.sleep(2 * (attempt + 1))
                continue
        except requests.RequestException:
            import time
            time.sleep(1)
    return dt, None


def parse_bi5(data: bytes, pair: str, hour_dt: datetime) -> list[dict]:
    """Parse .bi5 binary tick data."""
    try:
        raw = lzma.decompress(data)
    except lzma.LZMAError:
        return []

    point_value = POINT_VALUES.get(pair, 1e5)
    ticks = []
    row_size = 20
    for i in range(0, len(raw), row_size):
        if i + row_size > len(raw):
            break
        ms_offset, ask_raw, bid_raw, ask_vol, bid_vol = struct.unpack(
            ">IIIff", raw[i : i + row_size]
        )
        timestamp = hour_dt + timedelta(milliseconds=ms_offset)
        mid = ((ask_raw + bid_raw) / 2) / point_value
        ticks.append({"datetime": timestamp, "mid": mid, "vol": ask_vol + bid_vol})
    return ticks


def download_pair(
    pair: str,
    start_date: str,
    end_date: str,
    timeframe_minutes: int = 15,
    output_dir: str = "data/raw",
    max_workers: int = 5,
) -> str:
    """Download pair data — low concurrency to avoid rate limits."""
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, f"{pair}_{timeframe_minutes}m.csv")

    if os.path.exists(output_file):
        existing = pd.read_csv(output_file)
        if len(existing) > 10000:
            print(f"  {pair}: Already exists ({len(existing)} bars). Skipping.", flush=True)
            return output_file

    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")

    # Generate all trading hours (skip weekends)
    hours = []
    current = start
    while current < end:
        if current.weekday() < 5:  # Mon-Fri
            hours.append(current)
        current += timedelta(hours=1)

    total = len(hours)
    print(f"  {pair}: Downloading {total} hours ({start_date} to {end_date})...", flush=True)

    # Download with limited concurrency — process in batches
    all_ticks = []
    completed = 0
    data_hours = 0
    batch_size = 100

    session = make_session()

    for batch_start in range(0, total, batch_size):
        batch = hours[batch_start : batch_start + batch_size]

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(download_hour, session, pair, dt): dt for dt in batch
            }
            for future in as_completed(futures):
                dt, data = future.result()
                completed += 1
                if data:
                    ticks = parse_bi5(data, pair, dt)
                    all_ticks.extend(ticks)
                    data_hours += 1

        if completed % 500 == 0 or batch_start + batch_size >= total:
            pct = completed / total * 100
            print(f"    {pair}: {pct:.0f}% ({completed}/{total}, {data_hours} hours with data)", flush=True)

    if not all_ticks:
        print(f"  {pair}: No data downloaded!", flush=True)
        return ""

    print(f"  {pair}: Processing {len(all_ticks)} ticks into {timeframe_minutes}m bars...", flush=True)

    df = pd.DataFrame(all_ticks)
    df.set_index("datetime", inplace=True)
    df.sort_index(inplace=True)

    rule = f"{timeframe_minutes}min"
    ohlcv = df["mid"].resample(rule).agg(
        open="first", high="max", low="min", close="last"
    )
    ohlcv["volume"] = df["vol"].resample(rule).sum()
    ohlcv.dropna(subset=["open"], inplace=True)

    ohlcv.to_csv(output_file)
    print(f"  {pair}: Saved {len(ohlcv)} bars to {output_file}", flush=True)
    return output_file


def download_all():
    """Download data for all configured pairs sequentially."""
    print("=" * 60, flush=True)
    print("DUKASCOPY DATA DOWNLOADER", flush=True)
    print(f"Pairs: {', '.join(config.ALL_PAIRS)}", flush=True)
    print(f"Period: {config.BACKTEST_START} to {config.BACKTEST_END}", flush=True)
    print(f"Timeframe: {config.TIMEFRAME_MINUTES}m", flush=True)
    print("=" * 60, flush=True)

    data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), config.DATA_DIR)

    for pair in config.ALL_PAIRS:
        download_pair(
            pair=pair,
            start_date=config.BACKTEST_START,
            end_date=config.BACKTEST_END,
            timeframe_minutes=config.TIMEFRAME_MINUTES,
            output_dir=data_dir,
        )
        print(flush=True)

    print("Download complete!", flush=True)


if __name__ == "__main__":
    download_all()
