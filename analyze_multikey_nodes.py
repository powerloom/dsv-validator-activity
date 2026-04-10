#!/usr/bin/env python3
"""Analyze nodes running multiple signer keys.

Reads from out/metrics_by_signer_day.csv and out/signer_node_cache.jsonl.
Outputs:
  - out/multikey_analysis.json
  - out/node_aggregate_metrics.csv
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

import pandas as pd


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


def analyze_multikey_nodes(df: pd.DataFrame, signer_map: dict[str, int]) -> dict:
    df = df.copy()
    df["node_id"] = df["signer"].map(signer_map)

    # Group signers by node
    node_signers: dict[int, list[str]] = defaultdict(list)
    for signer, node_id in signer_map.items():
        node_signers[node_id].append(signer)

    result = {}
    for node_id in sorted(node_signers.keys()):
        signers = node_signers[node_id]
        node_df = df[df["node_id"] == node_id]

        # Per-signer timeline
        signer_timelines = {}
        for s in signers:
            s_df = df[df["signer"] == s]
            if s_df.empty:
                signer_timelines[s] = {
                    "first_day": None,
                    "last_day": None,
                    "active_days": 0,
                    "total_submitted": 0,
                    "total_completed": 0,
                }
                continue

            signer_timelines[s] = {
                "first_day": int(s_df["day_id"].min()),
                "last_day": int(s_df["day_id"].max()),
                "active_days": int(s_df["day_id"].nunique()),
                "total_submitted": int(s_df["snapshot_batch_submitted"].sum()),
                "total_completed": int(s_df["batch_submissions_completed"].sum()),
            }

        # Overlap analysis: days where both signers were active
        if len(signers) == 2:
            s1_days = set(df[df["signer"] == signers[0]]["day_id"].tolist())
            s2_days = set(df[df["signer"] == signers[1]]["day_id"].tolist())
            overlap_days = sorted(s1_days & s2_days)
            s1_only = sorted(s1_days - s2_days)
            s2_only = sorted(s2_days - s1_days)
        else:
            overlap_days = []
            s1_only = []
            s2_only = []

        result[f"node_{node_id}"] = {
            "node_id": node_id,
            "signer_count": len(signers),
            "signers": signers,
            "signer_timelines": signer_timelines,
            "overlap_days": overlap_days,
            "overlap_day_count": len(overlap_days),
            "signer_1_only_days": s1_only,
            "signer_2_only_days": s2_only,
            "pattern": (
                "concurrent" if len(overlap_days) > 5
                else "rotation" if len(overlap_days) <= 5 and len(signers) == 2
                else "single"
            ),
        }

    return result


def build_node_aggregate_metrics(df: pd.DataFrame, signer_map: dict[str, int]) -> pd.DataFrame:
    df = df.copy()
    df["node_id"] = df["signer"].map(signer_map)

    agg = (
        df.groupby("node_id")
        .agg(
            total_submitted=("snapshot_batch_submitted", "sum"),
            total_completed=("batch_submissions_completed", "sum"),
            active_days=("day_id", "nunique"),
            signer_count=("signer", "nunique"),
            mean_daily_submitted=("snapshot_batch_submitted", "mean"),
            std_daily_submitted=("snapshot_batch_submitted", "std"),
        )
        .reset_index()
    )

    agg["completion_rate_pct"] = (agg["total_completed"] / agg["total_submitted"] * 100).round(4)
    agg["mean_daily_submitted"] = agg["mean_daily_submitted"].round(1)
    agg["std_daily_submitted"] = agg["std_daily_submitted"].round(1)

    return agg


def main():
    parser = argparse.ArgumentParser(description="Multi-key node analysis")
    parser.add_argument("--data-dir", default="out", help="Directory containing input/output files")
    parser.add_argument("--output-dir", default=None, help="Output directory (defaults to data-dir)")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    out_dir = Path(args.output_dir) if args.output_dir else data_dir

    signer_map = load_signer_node_map(data_dir / "signer_node_cache.jsonl")
    df = pd.read_csv(data_dir / "metrics_by_signer_day.csv")

    print("Analyzing multi-key nodes...")
    analysis = analyze_multikey_nodes(df, signer_map)
    with open(out_dir / "multikey_analysis.json", "w") as f:
        json.dump(analysis, f, indent=2)
    print(f"  Wrote {out_dir}/multikey_analysis.json")

    multi = {k: v for k, v in analysis.items() if v["signer_count"] > 1}
    print(f"  {len(multi)} multi-signer nodes:")
    for k, v in sorted(multi.items()):
        print(f"    Node {v['node_id']}: {v['signer_count']} signers, "
              f"pattern={v['pattern']}, overlap_days={v['overlap_day_count']}")

    print("Building node aggregate metrics...")
    node_agg = build_node_aggregate_metrics(df, signer_map)
    node_agg.to_csv(out_dir / "node_aggregate_metrics.csv", index=False)
    print(f"  Wrote {out_dir}/node_aggregate_metrics.csv ({len(node_agg)} nodes)")

    print("\nPer-node aggregate:")
    for _, row in node_agg.sort_values("node_id").iterrows():
        print(f"  Node {int(row['node_id']):>2}: "
              f"submitted={int(row['total_submitted']):>6}  "
              f"completed={int(row['total_completed']):>6}  "
              f"completion={row['completion_rate_pct']:.2f}%  "
              f"signers={int(row['signer_count'])}")


if __name__ == "__main__":
    main()
