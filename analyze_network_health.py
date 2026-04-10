#!/usr/bin/env python3
"""Network-level health metrics and anomaly detection.

Reads from out/ JSONL logs and summary files.
Outputs:
  - out/network_health_daily.csv
  - out/anomaly_report.json
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


def build_daily_health(
    priorities: list[dict],
    submissions: list[dict],
    completed: list[dict],
) -> pd.DataFrame:
    # Aggregate per day
    def agg_day(records, count_col="count"):
        day_counts = defaultdict(int)
        for r in records:
            day_counts[r["day_id"]] += 1
        return day_counts

    pa_by_day = agg_day(priorities)
    sub_by_day = agg_day(submissions)
    comp_by_day = agg_day(completed)

    # Unique signers per day from submissions
    sub_signers_by_day: dict[int, set] = defaultdict(set)
    for r in submissions:
        sub_signers_by_day[r["day_id"]].add(r["signer_from"])

    # Validator count per day from priorities
    vcount_by_day: dict[int, int] = {}
    for r in priorities:
        day = r["day_id"]
        vc = r.get("validator_count", 0)
        if day not in vcount_by_day:
            vcount_by_day[day] = vc

    # Unique epochs per day from priorities
    epochs_by_day: dict[int, set] = defaultdict(set)
    for r in priorities:
        epochs_by_day[r["day_id"]].add(r["epoch_id"])

    all_days = sorted(set(pa_by_day) | set(sub_by_day) | set(comp_by_day))

    rows = []
    for day in all_days:
        pa = pa_by_day.get(day, 0)
        sub = sub_by_day.get(day, 0)
        comp = comp_by_day.get(day, 0)
        n_signers = len(sub_signers_by_day.get(day, set()))
        n_epochs = len(epochs_by_day.get(day, set()))
        vc = vcount_by_day.get(day, 0)
        completion_rate = round(comp / sub * 100, 4) if sub else 0

        rows.append({
            "day_id": day,
            "priorities_assigned": pa,
            "snapshot_batch_submitted": sub,
            "batch_submissions_completed": comp,
            "unique_epochs": n_epochs,
            "unique_signers": n_signers,
            "validator_count": vc,
            "completion_rate_pct": completion_rate,
        })

    return pd.DataFrame(rows)


def detect_anomalies(daily: pd.DataFrame, pa_by_day_raw: dict) -> dict:
    anomalies = []

    baseline_pa = 7200
    baseline_days = daily[daily["priorities_assigned"] == baseline_pa]
    baseline_signers = baseline_days["unique_signers"].mode().iloc[0] if len(baseline_days) > 0 else 0

    # Day 14 anomaly
    day14 = daily[daily["day_id"] == 14]
    if not day14.empty:
        d14 = day14.iloc[0]
        pa14 = int(d14["priorities_assigned"])
        if pa14 > baseline_pa * 1.1:
            delta = pa14 - baseline_pa
            pct_above = round((pa14 / baseline_pa - 1) * 100, 1)
            anomalies.append({
                "day_id": 14,
                "type": "priorities_assigned_spike",
                "value": pa14,
                "baseline": baseline_pa,
                "delta": delta,
                "pct_above_baseline": pct_above,
                "unique_signers": int(d14["unique_signers"]),
                "baseline_signers": int(baseline_signers),
                "unique_epochs": int(d14["unique_epochs"]),
                "validator_count": int(d14["validator_count"]),
                "analysis": (
                    f"Day 14 had {pa14} PrioritiesAssigned ({pct_above}% above {baseline_pa} baseline). "
                    f"Unique signers: {int(d14['unique_signers'])} vs baseline {int(baseline_signers)}. "
                    f"Unique epochs: {int(d14['unique_epochs'])}. "
                    f"This is likely caused by an increased number of epochs in this day "
                    f"(more L2 blocks produced), since validator_count={int(d14['validator_count'])} "
                    f"matches the baseline."
                ),
            })

    # Low-activity days
    for _, row in daily.iterrows():
        pa = row["priorities_assigned"]
        if pa > 0 and pa < baseline_pa * 0.95:
            anomalies.append({
                "day_id": int(row["day_id"]),
                "type": "low_priorities",
                "value": int(pa),
                "baseline": baseline_pa,
                "pct_below_baseline": round((1 - pa / baseline_pa) * 100, 1),
                "unique_epochs": int(row["unique_epochs"]),
                "analysis": (
                    f"Day {int(row['day_id'])} had only {int(pa)} PrioritiesAssigned "
                    f"({round((1 - pa / baseline_pa) * 100, 1)}% below {baseline_pa} baseline). "
                    f"Likely fewer L2 blocks produced on this day."
                ),
            })

    # Completion rate drops
    mean_completion = daily["completion_rate_pct"].mean()
    for _, row in daily.iterrows():
        cr = row["completion_rate_pct"]
        if cr < mean_completion - 2 * daily["completion_rate_pct"].std():
            anomalies.append({
                "day_id": int(row["day_id"]),
                "type": "low_completion_rate",
                "value": round(float(cr), 2),
                "network_mean": round(float(mean_completion), 2),
                "analysis": (
                    f"Day {int(row['day_id'])} completion rate {cr:.2f}% is significantly below "
                    f"network mean {mean_completion:.2f}%."
                ),
            })

    # Validator count changes
    vc_prev = None
    for _, row in daily.sort_values("day_id").iterrows():
        vc = int(row["validator_count"])
        if vc_prev is not None and vc != vc_prev:
            anomalies.append({
                "day_id": int(row["day_id"]),
                "type": "validator_count_change",
                "value": vc,
                "previous": vc_prev,
                "analysis": (
                    f"Validator count changed from {vc_prev} to {vc} on day {int(row['day_id'])}."
                ),
            })
        vc_prev = vc

    # Trend analysis
    first_half = daily[daily["day_id"] <= 15]["completion_rate_pct"].mean()
    second_half = daily[daily["day_id"] > 15]["completion_rate_pct"].mean()
    trend = "stable"
    delta = second_half - first_half
    if abs(delta) > 0.5:
        trend = "declining" if delta < 0 else "improving"

    report = {
        "total_anomalies": len(anomalies),
        "anomalies": anomalies,
        "network_summary": {
            "mean_daily_priorities_assigned": round(float(daily["priorities_assigned"].mean()), 1),
            "mean_daily_submitted": round(float(daily["snapshot_batch_submitted"].mean()), 1),
            "mean_daily_completed": round(float(daily["batch_submissions_completed"].mean()), 1),
            "mean_completion_rate_pct": round(float(mean_completion), 4),
            "overall_completion_rate_pct": round(
                daily["batch_submissions_completed"].sum() / daily["snapshot_batch_submitted"].sum() * 100, 4
            ),
            "completion_trend_first_half": round(float(first_half), 4),
            "completion_trend_second_half": round(float(second_half), 4),
            "completion_trend": trend,
            "days_with_full_7200_epochs": int((daily["priorities_assigned"] == 7200).sum()),
            "days_with_anomaly": int((daily["priorities_assigned"] != 7200).sum()),
        },
    }

    return report


def main():
    parser = argparse.ArgumentParser(description="Network health analysis")
    parser.add_argument("--data-dir", default="out", help="Directory containing input/output files")
    parser.add_argument("--output-dir", default=None, help="Output directory (defaults to data-dir)")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    out_dir = Path(args.output_dir) if args.output_dir else data_dir

    print("Loading JSONL logs...")
    priorities = load_jsonl(data_dir / "logs_priorities_assigned.jsonl")
    submissions = load_jsonl(data_dir / "logs_snapshot_batch_submitted.jsonl")
    completed = load_jsonl(data_dir / "logs_batch_submissions_completed.jsonl")
    print(f"  {len(priorities)} PA, {len(submissions)} SBS, {len(completed)} BSC")

    print("Building daily health metrics...")
    daily = build_daily_health(priorities, submissions, completed)
    daily.to_csv(out_dir / "network_health_daily.csv", index=False)
    print(f"  Wrote {out_dir}/network_health_daily.csv ({len(daily)} days)")

    print("Detecting anomalies...")
    pa_by_day_raw = {r["day_id"]: r.get("validator_count", 0) for r in priorities}
    report = detect_anomalies(daily, pa_by_day_raw)
    with open(out_dir / "anomaly_report.json", "w") as f:
        json.dump(report, f, indent=2)
    print(f"  Wrote {out_dir}/anomaly_report.json ({report['total_anomalies']} anomalies)")

    ns = report["network_summary"]
    print(f"\nNetwork summary:")
    print(f"  Mean daily PA: {ns['mean_daily_priorities_assigned']}")
    print(f"  Mean completion rate: {ns['mean_completion_rate_pct']}%")
    print(f"  Overall completion rate: {ns['overall_completion_rate_pct']}%")
    print(f"  Completion trend: {ns['completion_trend']} "
          f"(H1={ns['completion_trend_first_half']}% vs H2={ns['completion_trend_second_half']}%)")
    print(f"  Days with 7200 epochs: {ns['days_with_full_7200_epochs']}/30")


if __name__ == "__main__":
    main()
