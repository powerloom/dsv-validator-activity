#!/usr/bin/env python3
"""Epoch-granular validator participation and consensus analysis.

Reads from raw JSONL logs in out/.
Outputs:
  - out/epoch_participation.csv
  - out/submission_latency.csv
  - out/participation_summary.json
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

import pandas as pd


def load_jsonl(path: Path) -> list[dict]:
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def build_epoch_participation(priorities: list[dict], submissions: list[dict]) -> pd.DataFrame:
    # Set of epochs that were assigned
    assigned_epochs = {r["epoch_id"] for r in priorities}

    # Build epoch -> {signer} map from submissions
    epoch_signers: dict[int, set[str]] = defaultdict(set)
    for r in submissions:
        epoch_signers[r["epoch_id"]].add(r["signer_from"])

    # Build participation records: one row per epoch
    all_epochs = sorted(assigned_epochs)
    rows = []
    for epoch_id in all_epochs:
        signers = epoch_signers.get(epoch_id, set())
        rows.append({
            "epoch_id": epoch_id,
            "assigned": True,
            "submitter_count": len(signers),
            "submitters": ";".join(sorted(signers)) if signers else "",
            "missed": len(signers) == 0,
        })

    return pd.DataFrame(rows)


def build_submission_latency(priorities: list[dict], submissions: list[dict]) -> pd.DataFrame:
    # Build epoch -> first assigned timestamp
    epoch_pa_ts: dict[int, int] = {}
    epoch_day: dict[int, int] = {}
    epoch_vcount: dict[int, int] = {}
    for r in priorities:
        eid = r["epoch_id"]
        if eid not in epoch_pa_ts or r["timestamp"] < epoch_pa_ts[eid]:
            epoch_pa_ts[eid] = r["timestamp"]
            epoch_day[eid] = r["day_id"]
            epoch_vcount[eid] = r.get("validator_count", 0)

    # Build signer -> epoch -> min submission timestamp
    signer_epoch_ts: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(lambda: float("inf")))
    for r in submissions:
        signer_epoch_ts[r["signer_from"]][r["epoch_id"]] = min(
            signer_epoch_ts[r["signer_from"]][r["epoch_id"]], r["timestamp"]
        )

    rows = []
    for signer, epochs in signer_epoch_ts.items():
        for epoch_id, sub_ts in epochs.items():
            pa_ts = epoch_pa_ts.get(epoch_id)
            if pa_ts is not None:
                latency = sub_ts - pa_ts
                rows.append({
                    "epoch_id": epoch_id,
                    "signer": signer,
                    "pa_timestamp": pa_ts,
                    "submit_timestamp": sub_ts,
                    "latency_seconds": latency,
                    "day_id": epoch_day.get(epoch_id, 0),
                })

    return pd.DataFrame(rows)


def compute_participation_summary(
    participation: pd.DataFrame,
    latency: pd.DataFrame,
) -> dict:
    total_epochs = len(participation)
    epochs_with_subs = int((participation["submitter_count"] > 0).sum())
    epochs_missed = int(participation["missed"].sum())

    submitter_counts = participation["submitter_count"]
    hist = submitter_counts.value_counts().sort_index().to_dict()
    hist = {int(k): int(v) for k, v in hist.items()}

    summary = {
        "total_epochs_assigned": total_epochs,
        "epochs_with_submissions": epochs_with_subs,
        "epochs_missed_completely": epochs_missed,
        "missed_pct": round(epochs_missed / total_epochs * 100, 4) if total_epochs else 0,
        "mean_submitters_per_epoch": round(submitter_counts.mean(), 3),
        "median_submitters_per_epoch": float(submitter_counts.median()),
        "max_submitters_per_epoch": int(submitter_counts.max()),
        "submitter_count_histogram": hist,
        "latency_stats": {},
    }

    if not latency.empty:
        lat = latency["latency_seconds"]
        summary["latency_stats"] = {
            "mean_seconds": round(float(lat.mean()), 2),
            "median_seconds": round(float(lat.median()), 2),
            "p95_seconds": round(float(lat.quantile(0.95)), 2),
            "p99_seconds": round(float(lat.quantile(0.99)), 2),
            "min_seconds": int(lat.min()),
            "max_seconds": int(lat.max()),
        }

    # Per-signer missed epochs
    all_signers = sorted(latency["signer"].unique()) if not latency.empty else []
    assigned_set = set(participation["epoch_id"].tolist())
    signer_epochs = defaultdict(set)
    for _, row in latency.iterrows():
        signer_epochs[row["signer"]].add(row["epoch_id"])

    signer_stats = {}
    for s in all_signers:
        submitted = len(signer_epochs[s])
        # Can't know exact assignments per signer, but report raw counts
        signer_stats[s] = {
            "epochs_submitted": submitted,
        }
    summary["per_signer_submission_counts"] = signer_stats

    return summary


def main():
    parser = argparse.ArgumentParser(description="Epoch participation analysis")
    parser.add_argument("--data-dir", default="out", help="Directory containing input/output files")
    parser.add_argument("--output-dir", default=None, help="Output directory (defaults to data-dir)")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    out_dir = Path(args.output_dir) if args.output_dir else data_dir

    print("Loading JSONL logs...")
    priorities = load_jsonl(data_dir / "logs_priorities_assigned.jsonl")
    submissions = load_jsonl(data_dir / "logs_snapshot_batch_submitted.jsonl")
    print(f"  {len(priorities)} PrioritiesAssigned, {len(submissions)} SnapshotBatchSubmitted")

    print("Building epoch participation matrix...")
    participation = build_epoch_participation(priorities, submissions)
    participation.to_csv(out_dir / "epoch_participation.csv", index=False)
    print(f"  Wrote {out_dir}/epoch_participation.csv ({len(participation)} epochs)")

    print("Computing submission latency...")
    latency = build_submission_latency(priorities, submissions)
    latency.to_csv(out_dir / "submission_latency.csv", index=False)
    print(f"  Wrote {out_dir}/submission_latency.csv ({len(latency)} rows)")

    print("Computing participation summary...")
    summary = compute_participation_summary(participation, latency)
    with open(out_dir / "participation_summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    print(f"  Wrote {out_dir}/participation_summary.json")

    print(f"\nSummary:")
    print(f"  Total epochs: {summary['total_epochs_assigned']}")
    print(f"  Epochs with submissions: {summary['epochs_with_submissions']}")
    print(f"  Missed completely: {summary['epochs_missed_completely']} ({summary['missed_pct']}%)")
    print(f"  Mean submitters/epoch: {summary['mean_submitters_per_epoch']}")
    if summary["latency_stats"]:
        ls = summary["latency_stats"]
        print(f"  Latency: median={ls['median_seconds']}s  p95={ls['p95_seconds']}s  p99={ls['p99_seconds']}s")


if __name__ == "__main__":
    main()
