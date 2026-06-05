#!/usr/bin/env python3
"""Compare two validator-activity export windows (e.g. days 1-30 vs 31-60).

Reads participation_summary.json from each output directory and prints a delta report.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_summary(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def pct(n: float, d: float) -> float | None:
    if d == 0:
        return None
    return round(n / d * 100, 4)


def main() -> None:
    p = argparse.ArgumentParser(description="Compare DSV validator activity windows")
    p.add_argument(
        "--baseline-dir",
        type=Path,
        default=Path("out"),
        help="Earlier window (e.g. days 1-30 export)",
    )
    p.add_argument(
        "--compare-dir",
        type=Path,
        required=True,
        help="Later window (e.g. days 31-60 export)",
    )
    p.add_argument(
        "--baseline-label",
        default="days-1-30",
        help="Label for the baseline window used in the output filename "
        "(window_comparison_vs_<label>.json). E.g. days-31-60.",
    )
    args = p.parse_args()

    base_path = args.baseline_dir / "participation_summary.json"
    cmp_path = args.compare_dir / "participation_summary.json"
    if not base_path.is_file():
        raise SystemExit(f"Missing {base_path} — run analyze_epoch_participation.py on baseline dir")
    if not cmp_path.is_file():
        raise SystemExit(f"Missing {cmp_path} — run analyze_epoch_participation.py on compare dir")

    base = load_summary(base_path)
    cmp = load_summary(cmp_path)

    base_assigned = base["total_epochs_assigned"]
    cmp_assigned = cmp["total_epochs_assigned"]
    base_submitted = base["epochs_with_submissions"]
    cmp_submitted = cmp["epochs_with_submissions"]
    base_missed_pct = base["missed_pct"]
    cmp_missed_pct = cmp["missed_pct"]

    base_completion = pct(base_submitted, base_assigned)
    cmp_completion = pct(cmp_submitted, cmp_assigned)

    base_lat = base.get("latency_stats", {})
    cmp_lat = cmp.get("latency_stats", {})

    report = {
        "baseline_dir": str(args.baseline_dir.resolve()),
        "compare_dir": str(args.compare_dir.resolve()),
        "baseline": {
            "epochs_assigned": base_assigned,
            "epochs_with_submissions": base_submitted,
            "completion_pct": base_completion,
            "missed_pct": base_missed_pct,
            "median_latency_s": base_lat.get("median_seconds"),
            "p95_latency_s": base_lat.get("p95_seconds"),
        },
        "compare": {
            "epochs_assigned": cmp_assigned,
            "epochs_with_submissions": cmp_submitted,
            "completion_pct": cmp_completion,
            "missed_pct": cmp_missed_pct,
            "median_latency_s": cmp_lat.get("median_seconds"),
            "p95_latency_s": cmp_lat.get("p95_seconds"),
        },
        "delta": {
            "completion_pct_points": (
                round(cmp_completion - base_completion, 4)
                if base_completion is not None and cmp_completion is not None
                else None
            ),
            "missed_pct_points": round(cmp_missed_pct - base_missed_pct, 4),
            "median_latency_s": (
                round(cmp_lat.get("median_seconds", 0) - base_lat.get("median_seconds", 0), 2)
                if base_lat and cmp_lat
                else None
            ),
            "p95_latency_s": (
                round(cmp_lat.get("p95_seconds", 0) - base_lat.get("p95_seconds", 0), 2)
                if base_lat and cmp_lat
                else None
            ),
        },
    }

    out_path = args.compare_dir / f"window_comparison_vs_{args.baseline_label}.json"
    out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(report, indent=2))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
