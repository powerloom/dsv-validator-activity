# Published validator activity reports

Committed **analysis outputs only** — not raw `eth_getLogs` dumps. Anyone can reproduce the full pipeline locally with an archive Powerloom L2 RPC.

## Layout

| Path | Protocol days | Contents |
|------|---------------|----------|
| [`days-1-30/`](days-1-30/) | 1–30 | `participation_summary.json`, `reliability_summary.json`, `metrics_summary.json`, `anomaly_report.json`, `day_boundaries.json`, `addresses_resolved.json` |
| [`days-31-60/`](days-31-60/) | 31–60 | Same (no anomaly report for this window yet), plus `window_comparison_vs_days-1-30.json` |
| [`combined-days-1-60/summary.json`](combined-days-1-60/summary.json) | 1–60 | Rolled-up metrics and reproduction commands |
| [`charts/`](charts/) | — | PNG outputs from `generate_visualizations.py` (commit after regen) |

## Reproduce from scratch

```bash
cd scripts/dsv-validator-activity   # or clone powerloom/dsv-validator-activity
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-viz.txt    # only for charts
export POWERLOOM_RPC_URL=https://...   # archive-capable JSON-RPC (chain 7869)

# Window 1 (hours-long)
python export_validator_activity.py --out ./out --day-start 1 --day-end 30 --fresh
python analyze_epoch_participation.py --data-dir out
python analyze_validator_reliability.py --data-dir out
python analyze_network_health.py --data-dir out
python analyze_multikey_nodes.py --data-dir out

# Window 2
python export_validator_activity.py --out ./out-days31-60 --day-start 31 --day-end 60 --fresh
python analyze_epoch_participation.py --data-dir out-days31-60
python analyze_validator_reliability.py --data-dir out-days31-60
python analyze_network_health.py --data-dir out-days31-60
python analyze_multikey_nodes.py --data-dir out-days31-60
python compare_windows.py --baseline-dir out --compare-dir out-days31-60

# Diff your outputs against this directory:
diff reports/days-1-30/participation_summary.json out/participation_summary.json
diff reports/days-31-60/participation_summary.json out-days31-60/participation_summary.json
```


## Generate charts

Requires analyzer CSV/JSONL under your export dir (not just the committed JSON summaries).

```bash
# Days 1–30 only → reports/charts/dsv_network_health_timeline.png
python generate_visualizations.py --data-dir out --day-end 30

# Days 31–60 only → reports/charts/dsv_network_health_timeline_days_31_60.png
python analyze_network_health.py --data-dir out3160/out3160   # or your 31–60 export dir
python generate_visualizations.py --data-dir out3160/out3160 --day-start 31 --day-end 60 \
  --filename-suffix _days_31_60 --skip-tally --charts timeline

# Full 60-day timeline (two export directories)
python generate_visualizations.py \
  --data-dir out \
  --merge-dir out-days31-60 \
  --day-start 1 --day-end 60 \
  --summary-json reports/combined-days-1-60/summary.json

# Slot radial (needs tally JSON; workspace path or --tally-dir)
python generate_visualizations.py --data-dir out --charts slots \
  --tally-dir /path/to/tally_reports/tallies
```

Outputs:

| PNG | Data |
|-----|------|
| `dsv_network_health_timeline.png` | `network_health_daily.csv` (days 1–30) |
| `dsv_network_health_timeline_days_31_60.png` | `network_health_daily.csv` (days 31–60) |
| `dsv_validator_constellation.png` | `node_aggregate_metrics.csv` |
| `dsv_latency_heatmap.png` | `submission_latency.csv` |
| `dsv_epoch_coverage_matrix.png` | `epoch_participation.csv` |
| `dsv_slot_distribution_radial.png` | `tally_reports/tallies/epoch_*.json` |

Copy PNGs to docs: `powerloom-docs/static/images/bds-agentic-workflow/dsv-mainnet-operations/`

## Verify on-chain

1. Confirm contracts in `addresses_resolved.json` match your target deployment.
2. Use `day_boundaries.json` block ranges to spot-check `eth_getLogs` for the window.
3. Epoch completion: `epochs_with_submissions / total_epochs_assigned` in `participation_summary.json`.

## Docs consumer

Public narrative: [Stability and Scale](https://docs.powerloom.io/docs/dsv-mainnet/stability-and-scale) (references these artifacts).

Last published: **2026-05-19** (days 31–60 analysis + chart tooling in-repo).
