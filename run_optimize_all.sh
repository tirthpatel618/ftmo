#!/bin/bash
# Run all strategy optimizations sequentially.
# Designed for GitHub Codespaces / cloud VMs.
#
# Usage: bash run_optimize_all.sh [max_combos] [max_workers]
#   max_combos: max parameter combinations per strategy (default 500)
#   max_workers: parallel backtest workers (default 4)

MAX_COMBOS=${1:-200}
MAX_WORKERS=${2:-4}

pip install backtrader rich 2>/dev/null

echo "=== FTMO Strategy Optimization ==="
echo "Max combos per strategy: $MAX_COMBOS"
echo "Workers: $MAX_WORKERS"
echo ""

mkdir -p results

for STRATEGY in london bb_squeeze fvg ensemble; do
    echo "=========================================="
    echo "Optimizing: $STRATEGY"
    echo "=========================================="
    python3 run_backtest.py optimize "$STRATEGY" "$MAX_COMBOS" 2>&1 | tee "results/optimize_${STRATEGY}.log"
    echo ""
done

echo "=== All optimizations complete ==="
echo "Results saved in results/"
