#!/usr/bin/env python3
"""Per-node reliability scoring across the 30-day validator activity period.

Reads from out/metrics_by_signer_day.csv and out/signer_node_cache.jsonl.
Outputs:
  - out/reliability_by_node_day.csv
  - out/reliability_summary.json
"""

import argparse
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd


class NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


def load_signer_node_map(path: str) -> dict[str, int]:
    mapping = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            mapping[rec["signer"]] = rec["node_id"]
    return mapping


def build_node_day_metrics(df: pd.DataFrame, signer_map: dict[str, int]) -> pd.DataFrame:
    df = df.copy()
    df["node_id"] = df["signer"].map(signer_map)

    node_day = (
        df.groupby(["node_id", "day_id"])
        .agg(
            total_submitted=("snapshot_batch_submitted", "sum"),
            total_completed=("batch_submissions_completed", "sum"),
            signer_count=("signer", "nunique"),
            signers=("signer", lambda x: sorted(x.tolist())),
        )
        .reset_index()
    )

    node_day["completion_ratio"] = node_day["total_completed"] / node_day["total_submitted"].replace(0, 1)
    node_day["completion_ratio"] = node_day["completion_ratio"].round(6)
    return node_day


def compute_reliability_summary(node_day: pd.DataFrame, total_days: int) -> dict:
    summary = {}
    for node_id in sorted(node_day["node_id"].unique()):
        node_rows = node_day[node_day["node_id"] == node_id]
        active_days = len(node_rows)
        total_submitted = node_rows["total_submitted"].sum()
        total_completed = node_rows["total_completed"].sum()

        uptime_pct = round(active_days / total_days * 100, 2)
        mean_daily = round(total_submitted / active_days, 1) if active_days else 0
        std_daily = round(node_rows["total_submitted"].std(), 1)
        overall_completion = round(total_completed / total_submitted * 100, 4) if total_submitted else 0

        summary[f"node_{node_id}"] = {
            "node_id": node_id,
            "active_days": active_days,
            "total_days": total_days,
            "uptime_pct": uptime_pct,
            "total_submitted": int(total_submitted),
            "total_completed": int(total_completed),
            "overall_completion_pct": overall_completion,
            "mean_daily_submitted": mean_daily,
            "std_daily_submitted": std_daily,
            "consistency_score": round(max(0, 100 - std_daily / mean_daily * 100), 2) if mean_daily else 0,
            "min_daily_submitted": int(node_rows["total_submitted"].min()),
            "max_daily_submitted": int(node_rows["total_submitted"].max()),
        }
    return summary


def main():
    parser = argparse.ArgumentParser(description="Validator reliability analysis")
    parser.add_argument("--data-dir", default="out", help="Directory containing input/output files")
    parser.add_argument("--output-dir", default=None, help="Output directory (defaults to data-dir)")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    out_dir = Path(args.output_dir) if args.output_dir else data_dir

    signer_map = load_signer_node_map(data_dir / "signer_node_cache.jsonl")
    df = pd.read_csv(data_dir / "metrics_by_signer_day.csv")

    total_days = df["day_id"].nunique()

    node_day = build_node_day_metrics(df, signer_map)

    # Drop list column for CSV output
    csv_cols = node_day.drop(columns=["signers"])
    csv_cols.to_csv(out_dir / "reliability_by_node_day.csv", index=False)

    summary = compute_reliability_summary(node_day, total_days)

    with open(out_dir / "reliability_summary.json", "w") as f:
        json.dump(summary, f, indent=2, cls=NumpyEncoder)

    print(f"Wrote {out_dir}/reliability_by_node_day.csv ({len(csv_cols)} rows)")
    print(f"Wrote {out_dir}/reliability_summary.json ({len(summary)} nodes)")
    for nid, metrics in sorted(summary.items(), key=lambda x: x[1]["node_id"]):
        print(f"  Node {metrics['node_id']:>2}: uptime={metrics['uptime_pct']:6.1f}%  "
              f"completion={metrics['overall_completion_pct']:.2f}%  "
              f"mean_daily={metrics['mean_daily_submitted']:.0f}  "
              f"std={metrics['std_daily_submitted']:.0f}")


if __name__ == "__main__":
    main()
