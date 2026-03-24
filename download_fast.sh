#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate

echo "Fast parallel download — 50 workers per pair, all pairs simultaneous"
echo ""

# EURUSD already done, download remaining 7 in parallel
PAIRS=("GBPUSD" "GBPJPY" "EURJPY" "USDCAD" "EURCHF" "AUDNZD" "EURGBP")

for pair in "${PAIRS[@]}"; do
    python3 -u -c "
import sys; sys.path.insert(0, '.')
from data.download import download_pair
download_pair('$pair', '2024-01-01', '2025-12-31', 15, 'data/raw', max_workers=50)
" > "data/raw/${pair}_log.txt" 2>&1 &
    echo "  Launched $pair (PID: $!)"
done

echo ""
echo "All 7 pairs downloading in parallel. Monitor with:"
echo "  tail -f data/raw/*_log.txt"
echo ""
echo "Or check progress with:"
echo "  ls -lh data/raw/*.csv"
echo ""
wait

echo "===== ALL DONE ====="
ls -lh data/raw/*.csv
echo ""
echo "Run backtests with: python3 run_backtest.py all"
