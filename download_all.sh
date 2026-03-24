#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate

# Clear old incomplete data
rm -f data/raw/*.csv

echo "Downloading 5 years of forex data..."
echo "Low concurrency to avoid rate limits. ~15-20 min per pair."
echo ""

# Priority pairs first (London breakout + NY momentum)
PAIRS=("EURUSD" "GBPUSD" "GBPJPY" "EURJPY" "USDCAD" "EURCHF" "AUDNZD" "EURGBP")

for pair in "${PAIRS[@]}"; do
    python3 -u -c "
import sys; sys.path.insert(0, '.')
from data.download import download_pair
download_pair('$pair', '2024-01-01', '2025-12-31', 15, 'data/raw', max_workers=5)
"
    echo ""
done

echo "===== VERIFYING DATA ====="
python3 -u -c "
import pandas as pd, os
for f in sorted(os.listdir('data/raw')):
    if f.endswith('.csv'):
        df = pd.read_csv(f'data/raw/{f}', index_col=0, parse_dates=True)
        gaps = df.index.to_series().diff()
        big_gaps = len(gaps[gaps > pd.Timedelta(days=3)])
        print(f'{f}: {len(df):>6} bars | {df.index[0].date()} to {df.index[-1].date()} | {big_gaps} gaps>3d')
"
echo ""
echo "Run backtests with: python3 run_backtest.py all"
