#!/usr/bin/env python3
"""
Neo-futuristic charts for DSV validator activity exports.

Prerequisites (per --data-dir export output):
  analyze_epoch_participation.py
  analyze_network_health.py
  analyze_multikey_nodes.py

Optional: tally_reports/ for slot radial chart (--tally-dir).

See reports/README.md and docs/dsv-production-visualization-plan.md (workspace daily notes).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.patches import Circle

_PKG_DIR = Path(__file__).resolve().parent

COLORS = {
    "background": "#0a0a0a",
    "background_alt": "#111111",
    "grid": "#1a3a3a",
    "primary_green": "#00ff88",
    "secondary_green": "#00cc6a",
    "dim_green": "#00aa44",
    "dark_green": "#006633",
    "cyan": "#00ffff",
    "dim_cyan": "#00aaaa",
    "purple": "#9966ff",
    "white": "#ffffff",
    "text": "#e0e0e0",
    "text_dim": "#888888",
}


def setup_neo_futuristic_style() -> None:
    plt.style.use("dark_background")
    plt.rcParams.update(
        {
            "figure.facecolor": COLORS["background"],
            "axes.facecolor": COLORS["background_alt"],
            "axes.edgecolor": COLORS["grid"],
            "axes.labelcolor": COLORS["text"],
            "text.color": COLORS["text"],
            "xtick.color": COLORS["text_dim"],
            "ytick.color": COLORS["text_dim"],
            "grid.color": COLORS["grid"],
            "grid.alpha": 0.3,
            "axes.grid": True,
            "font.family": "sans-serif",
        }
    )


def add_glow_effect(ax, x, y, color, size=100, alpha_base=0.4, rings=4) -> None:
    for i in range(rings, 0, -1):
        ax.scatter(
            x,
            y,
            s=size * (i**1.5) * 3,
            c=color,
            alpha=alpha_base / i,
            edgecolors="none",
            zorder=1,
        )
    ax.scatter(
        x,
        y,
        s=size,
        c=color,
        alpha=1.0,
        edgecolors=COLORS["white"],
        linewidths=1.5,
        zorder=10,
    )


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(f"Missing {path} — run prerequisite analyzers first.")
    return pd.read_csv(path)


def _nodes_from_reliability(data_dir: Path) -> pd.DataFrame:
    rel_path = data_dir / "reliability_summary.json"
    if not rel_path.is_file():
        raise FileNotFoundError(
            f"Missing {data_dir / 'node_aggregate_metrics.csv'} and {rel_path}. "
            "Run analyze_multikey_nodes.py or analyze_validator_reliability.py."
        )
    with open(rel_path, encoding="utf-8") as f:
        rel = json.load(f)
    rows = []
    for _key, m in rel.items():
        rows.append(
            {
                "node_id": m["node_id"],
                "total_submitted": m["total_submitted"],
                "total_completed": m["total_completed"],
                "active_days": m["active_days"],
                "completion_rate_pct": m["overall_completion_pct"],
            }
        )
    return pd.DataFrame(rows)


def load_validator_data(
    data_dirs: list[Path],
    summary_path: Path | None,
) -> dict:
    """Load and optionally merge CSV outputs from one or two export directories."""
    if not data_dirs:
        raise ValueError("At least one --data-dir is required")

    health_parts: list[pd.DataFrame] = []
    participation_parts: list[pd.DataFrame] = []
    latency_parts: list[pd.DataFrame] = []
    signer_frames: list[pd.DataFrame] = []

    for d in data_dirs:
        print(f"  Loading from {d} ...")
        health_parts.append(_read_csv(d / "network_health_daily.csv"))
        participation_parts.append(_read_csv(d / "epoch_participation.csv"))
        latency_parts.append(_read_csv(d / "submission_latency.csv"))
        signer_path = d / "metrics_by_signer_day.csv"
        if signer_path.is_file():
            signer_frames.append(pd.read_csv(signer_path))

    health = pd.concat(health_parts, ignore_index=True).sort_values("day_id")
    participation = pd.concat(participation_parts, ignore_index=True)
    latency = pd.concat(latency_parts, ignore_index=True)

    nodes_df: pd.DataFrame | None = None
    if len(data_dirs) > 1 and signer_frames:
        from analyze_multikey_nodes import build_node_aggregate_metrics, load_signer_node_map

        merged = pd.concat(signer_frames, ignore_index=True)
        cache = data_dirs[-1] / "signer_node_cache.jsonl"
        if not cache.is_file():
            cache = data_dirs[0] / "signer_node_cache.jsonl"
        signer_map = load_signer_node_map(str(cache))
        nodes_df = build_node_aggregate_metrics(merged, signer_map)
    else:
        node_csv = data_dirs[-1] / "node_aggregate_metrics.csv"
        if node_csv.is_file():
            nodes_df = pd.read_csv(node_csv)
        else:
            nodes_df = _nodes_from_reliability(data_dirs[-1])

    if summary_path and summary_path.is_file():
        with open(summary_path, encoding="utf-8") as f:
            summary = json.load(f)
        # combined-days-* summaries nest rollups under "combined"/"windows";
        # normalize to the flat keys the timeline stats box expects.
        if "total_epochs_assigned" not in summary and "combined" in summary:
            combined = summary["combined"]
            windows = summary.get("windows", {})
            last_window = list(windows.values())[-1] if windows else {}
            summary = {
                "total_epochs_assigned": combined.get("epochs_assigned", 0),
                "epochs_with_submissions": combined.get("epochs_with_submissions", 0),
                "epochs_missed_completely": combined.get("missed_epochs", 0),
                "missed_pct": combined.get("missed_pct", 0),
                "latency_stats": {
                    "median_seconds": last_window.get("latency_median_s", 0),
                    "p95_seconds": last_window.get("latency_p95_s", 0),
                },
            }
    else:
        with open(data_dirs[-1] / "participation_summary.json", encoding="utf-8") as f:
            summary = json.load(f)
        if len(data_dirs) > 1 and len(data_dirs) == 2:
            with open(data_dirs[0] / "participation_summary.json", encoding="utf-8") as f:
                s0 = json.load(f)
            summary = {
                "total_epochs_assigned": s0["total_epochs_assigned"] + summary["total_epochs_assigned"],
                "epochs_with_submissions": s0["epochs_with_submissions"] + summary["epochs_with_submissions"],
                "epochs_missed_completely": s0["epochs_missed_completely"] + summary["epochs_missed_completely"],
                "missed_pct": round(
                    (s0["epochs_missed_completely"] + summary["epochs_missed_completely"])
                    / (s0["total_epochs_assigned"] + summary["total_epochs_assigned"])
                    * 100,
                    4,
                ),
                "latency_stats": summary.get("latency_stats", {}),
            }
            summary["latency_stats"] = summary.get("latency_stats") or s0.get("latency_stats", {})

    return {
        "health": health,
        "nodes": nodes_df,
        "participation": participation,
        "latency": latency,
        "summary": summary,
    }


def load_tally_data(tally_dir: Path, sample_size: int = 500) -> dict:
    print(f"Loading tally data from {tally_dir} (sample {sample_size})...")
    tally_files = sorted(tally_dir.glob("epoch_*.json"))
    if not tally_files:
        raise FileNotFoundError(f"No epoch_*.json under {tally_dir}")

    if len(tally_files) > sample_size:
        indices = np.linspace(0, len(tally_files) - 1, sample_size, dtype=int)
        sampled_files = [tally_files[i] for i in indices]
    else:
        sampled_files = tally_files

    slot_activity: dict[int, int] = defaultdict(int)
    epoch_data = []

    for f in sampled_files:
        with open(f, encoding="utf-8") as fp:
            data = json.load(fp)
        submissions = data.get("submission_counts", {})
        for slot_id, count in submissions.items():
            slot_activity[int(slot_id)] += count
        epoch_data.append(
            {
                "epoch_id": data.get("epoch_id", 0),
                "eligible_nodes": data.get("eligible_nodes_count", 0),
                "active_validators": data.get("total_validators", 0),
                "total_submissions": sum(submissions.values()),
            }
        )

    slots = pd.DataFrame(
        [{"slot_id": k, "total_submissions": v} for k, v in sorted(slot_activity.items())]
    )
    return {"epochs": pd.DataFrame(epoch_data), "slots": slots}


def visualize_network_health_timeline(
    validator_data: dict,
    out_dir: Path,
    day_start: int,
    day_end: int,
    title_suffix: str,
    filename_suffix: str = "",
) -> None:
    print("Generating network health timeline...")
    health = validator_data["health"]
    summary = validator_data["summary"]
    health = health[(health["day_id"] >= day_start) & (health["day_id"] <= day_end)].copy()
    if health.empty:
        print("  Skip: no network_health_daily rows in day range")
        return

    days = health["day_id"].values
    completion_rates = health["completion_rate_pct"].values
    rolling_avg = pd.Series(completion_rates).rolling(window=7, min_periods=1).mean()
    span = int(day_end - day_start + 1)

    fig, ax = plt.subplots(figsize=(14, 7), facecolor=COLORS["background"])
    ax.set_facecolor(COLORS["background_alt"])

    ax.fill_between(days, 95, completion_rates, alpha=0.15, color=COLORS["primary_green"])
    ax.fill_between(days, 95, completion_rates, alpha=0.3, color=COLORS["secondary_green"])
    for i in range(3, 0, -1):
        ax.plot(days, completion_rates, color=COLORS["primary_green"], alpha=0.1 * i, linewidth=2 + i * 2)
    ax.plot(days, completion_rates, color=COLORS["primary_green"], linewidth=2.5, label="Daily Completion Rate")
    ax.plot(days, rolling_avg, color=COLORS["cyan"], linewidth=2, linestyle="--", label="7-Day Average")
    ax.axhline(y=99, color=COLORS["purple"], linestyle=":", linewidth=2, alpha=0.6, label="99% Target")

    milestones = [
        (day_start, "Genesis" if day_start == 1 else f"Day {day_start}", COLORS["dim_cyan"]),
        (day_end, f"Day {day_end}", COLORS["primary_green"]),
    ]
    for day, label, color in milestones:
        match = health[health["day_id"] == day]
        if match.empty:
            continue
        rate = float(match["completion_rate_pct"].iloc[0])
        add_glow_effect(ax, day, rate, color, size=150, alpha_base=0.5, rings=5)
        ax.annotate(label, xy=(day, rate), xytext=(day, rate + 1.2), fontsize=10, color=color, ha="center")

    latency_stats = summary.get("latency_stats", {})
    completion_pct = 100 - summary.get("missed_pct", 0)
    stats_text = (
        f"Completion Stats\n"
        f"• Epochs: {summary.get('total_epochs_assigned', 0):,}\n"
        f"• Submitted: {summary.get('epochs_with_submissions', 0):,}\n"
        f"• Rate: {completion_pct:.2f}%\n"
        f"• Median latency: {latency_stats.get('median_seconds', 0):.0f}s"
    )
    ax.text(
        0.02,
        0.98,
        stats_text,
        transform=ax.transAxes,
        fontsize=10,
        color=COLORS["text"],
        va="top",
        bbox=dict(boxstyle="round,pad=0.5", facecolor=COLORS["background"], edgecolor=COLORS["grid"], alpha=0.9),
    )

    ax.set_title(
        f"DSV Network: {title_suffix}\nBatch Submission Completion Rate",
        fontsize=16,
        fontweight="bold",
        color=COLORS["primary_green"],
        pad=16,
    )
    ax.set_xlabel("Protocol day", fontsize=12)
    ax.set_ylabel("Completion rate (%)", fontsize=12)
    ax.set_xlim(day_start - 1, day_end + 2)
    ax.set_ylim(94, 100.5)
    ax.legend(loc="lower right", framealpha=0.9, facecolor=COLORS["background"])

    out_path = out_dir / f"dsv_network_health_timeline{filename_suffix}.png"
    plt.tight_layout()
    plt.savefig(out_path, dpi=200, facecolor=COLORS["background"], bbox_inches="tight", pad_inches=0.3)
    plt.close()
    print(f"  Saved: {out_path.name}")


def visualize_validator_constellation(
    validator_data: dict,
    out_dir: Path,
    min_active_days: int,
    total_days: int,
) -> None:
    print("Generating validator constellation...")
    nodes = validator_data["nodes"]
    active_nodes = nodes[nodes["active_days"] >= min_active_days].copy()
    active_nodes = active_nodes.sort_values("completion_rate_pct", ascending=False)
    if active_nodes.empty:
        print("  Skip: no nodes meet min_active_days")
        return

    n_nodes = len(active_nodes)
    angles = np.linspace(0, 2 * np.pi, n_nodes, endpoint=False)
    radius = 3.5

    def get_node_color(rate: float) -> str:
        if rate >= 99.8:
            return COLORS["primary_green"]
        if rate >= 99.0:
            return COLORS["secondary_green"]
        if rate >= 98.0:
            return COLORS["dim_green"]
        return COLORS["dim_cyan"]

    fig, ax = plt.subplots(figsize=(10, 10), facecolor=COLORS["background"])
    ax.set_facecolor(COLORS["background"])

    for i, (_, node) in enumerate(active_nodes.iterrows()):
        angle = angles[i]
        x, y = radius * np.cos(angle), radius * np.sin(angle)
        frac = node["active_days"] / max(total_days, 1)
        ax.plot([0, x], [0, y], color=COLORS["grid"], linewidth=1 + frac * 3, alpha=0.3 + frac * 0.4, zorder=1)

    for r, alpha in [(1.2, 0.1), (0.9, 0.2), (0.6, 0.4)]:
        ax.add_patch(Circle((0, 0), r, facecolor=COLORS["primary_green"], edgecolor="none", alpha=alpha, zorder=2))
    ax.text(0, 0, "POWERLOOM\nDSV", fontsize=14, fontweight="bold", color=COLORS["white"], ha="center", va="center", zorder=10)

    max_sub = max(active_nodes["total_submitted"].max(), 1)
    for i, (_, node) in enumerate(active_nodes.iterrows()):
        angle = angles[i]
        x, y = radius * np.cos(angle), radius * np.sin(angle)
        color = get_node_color(node["completion_rate_pct"])
        size = 800 + (node["total_submitted"] / max_sub) * 1200
        for j in range(4, 0, -1):
            ax.scatter(x, y, s=size * (j**1.2), c=color, alpha=0.15 / j, edgecolors="none", zorder=3)
        ax.scatter(x, y, s=size, c=color, edgecolors=COLORS["white"], linewidths=2, zorder=10, marker="h")
        lx, ly = 1.25 * radius * np.cos(angle), 1.25 * radius * np.sin(angle)
        ax.text(
            lx,
            ly,
            f"Node {int(node['node_id'])}\n{node['completion_rate_pct']:.2f}%\n{int(node['active_days'])}/{total_days} days",
            fontsize=9,
            color=color,
            ha="center",
            fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.3", facecolor=COLORS["background"], edgecolor=color, alpha=0.8),
        )

    ax.set_title(
        f"Validator Network Constellation\n{n_nodes} nodes | ≥{min_active_days} active days",
        fontsize=14,
        fontweight="bold",
        color=COLORS["primary_green"],
        pad=20,
    )
    ax.set_xlim(-5, 5)
    ax.set_ylim(-5, 5)
    ax.set_aspect("equal")
    ax.axis("off")
    out_path = out_dir / "dsv_validator_constellation.png"
    plt.tight_layout()
    plt.savefig(out_path, dpi=200, facecolor=COLORS["background"], bbox_inches="tight", pad_inches=0.3)
    plt.close()
    print(f"  Saved: {out_path.name}")


def visualize_latency_heatmap(validator_data: dict, out_dir: Path) -> None:
    print("Generating latency heatmap...")
    latency = validator_data["latency"].copy()
    summary = validator_data["summary"]
    if "pa_timestamp" not in latency.columns:
        print("  Skip: submission_latency.csv missing pa_timestamp")
        return

    latency["hour"] = pd.to_datetime(latency["pa_timestamp"], unit="s", utc=True).dt.hour
    hours = latency["hour"].values
    latencies = latency["latency_seconds"].values
    mask = latencies <= 250
    heatmap, xedges, yedges = np.histogram2d(
        hours[mask], latencies[mask], bins=[24, 50], range=[[0, 24], [0, 250]]
    )
    colors = [COLORS["background"], COLORS["grid"], COLORS["dim_green"], COLORS["secondary_green"], COLORS["primary_green"]]
    cmap = LinearSegmentedColormap.from_list("neon", colors, N=100)

    fig, ax = plt.subplots(figsize=(12, 7), facecolor=COLORS["background"])
    ax.imshow(
        heatmap.T,
        extent=[xedges[0], xedges[-1], yedges[0], yedges[-1]],
        origin="lower",
        aspect="auto",
        cmap=cmap,
        interpolation="gaussian",
    )

    ls = summary.get("latency_stats", {})
    median_lat = ls.get("median_seconds", 89)
    mean_lat = ls.get("mean_seconds", 88)
    p95_lat = ls.get("p95_seconds", 138)
    ax.axhline(y=median_lat, color=COLORS["cyan"], linewidth=2, label=f"Median: {median_lat:.0f}s")
    ax.axhline(y=mean_lat, color=COLORS["white"], linestyle="--", linewidth=1.5, alpha=0.7, label=f"Mean: {mean_lat:.0f}s")
    ax.axhline(y=p95_lat, color=COLORS["purple"], linestyle=":", linewidth=2, label=f"P95: {p95_lat:.0f}s")

    ax.set_title("Batch Submission Latency", fontsize=18, fontweight="bold", color=COLORS["primary_green"], pad=12)
    ax.set_xlabel("Hour of day (UTC)")
    ax.set_ylabel("Latency (seconds)")
    ax.set_xlim(0, 24)
    ax.set_ylim(0, 250)
    ax.legend(loc="upper left", framealpha=0.9)
    out_path = out_dir / "dsv_latency_heatmap.png"
    plt.tight_layout()
    plt.savefig(out_path, dpi=200, facecolor=COLORS["background"], bbox_inches="tight", pad_inches=0.3)
    plt.close()
    print(f"  Saved: {out_path.name}")


def visualize_epoch_coverage_matrix(
    validator_data: dict,
    out_dir: Path,
    day_start: int,
    day_end: int,
) -> None:
    print("Generating epoch coverage matrix...")
    participation = validator_data["participation"].copy()
    summary = validator_data["summary"]
    e0 = participation["epoch_id"].min()
    participation["day"] = (participation["epoch_id"] - e0) // 7200 + 1
    participation["hour"] = ((participation["epoch_id"] - e0) % 7200) // 300
    participation = participation[
        (participation["day"] >= day_start) & (participation["day"] <= day_end)
    ]
    pivot = participation.pivot_table(
        values="submitter_count", index="day", columns="hour", aggfunc="sum", fill_value=0
    )
    if pivot.empty:
        print("  Skip: empty coverage pivot")
        return

    colors = [COLORS["background"], COLORS["dark_green"], COLORS["dim_green"], COLORS["secondary_green"], COLORS["primary_green"]]
    cmap = LinearSegmentedColormap.from_list("activity", colors, N=100)
    fig, ax = plt.subplots(figsize=(14, max(6, pivot.shape[0] * 0.22)), facecolor=COLORS["background"])
    ax.imshow(pivot.values, cmap=cmap, aspect="auto", interpolation="nearest")
    ax.set_title(
        f"Epoch Coverage Matrix (days {day_start}–{day_end})",
        fontsize=16,
        fontweight="bold",
        color=COLORS["primary_green"],
        pad=12,
    )
    rate = 100 - summary.get("missed_pct", 0)
    ax.set_xlabel("Epoch block (5 epochs per cell)")
    ax.set_ylabel("Protocol day")
    out_path = out_dir / "dsv_epoch_coverage_matrix.png"
    plt.tight_layout()
    plt.savefig(out_path, dpi=200, facecolor=COLORS["background"], bbox_inches="tight", pad_inches=0.3)
    plt.close()
    print(f"  Saved: {out_path.name}")


def visualize_slot_distribution_radial(tally_data: dict, out_dir: Path) -> None:
    print("Generating slot distribution radial...")
    slots = tally_data["slots"]
    if slots.empty:
        print("  Skip: no slot data")
        return

    fig, ax = plt.subplots(figsize=(10, 10), facecolor=COLORS["background"])
    theta = np.linspace(0, 2 * np.pi, len(slots), endpoint=False)
    max_subs = max(slots["total_submissions"].max(), 1)
    radii = np.sqrt(slots["total_submissions"].values / max_subs) * 4 + 1
    bar_colors = [
        COLORS["primary_green"] if r["total_submissions"] >= 100 else COLORS["dim_green"] if r["total_submissions"] >= 10 else COLORS["grid"]
        for _, r in slots.iterrows()
    ]
    width = 2 * np.pi / len(slots) * 0.8
    ax.bar(theta, radii, width=width, bottom=2, color=bar_colors, alpha=0.7)
    active_slots = len(slots[slots["total_submissions"] > 0])
    ax.text(0, 0.3, f"{active_slots:,}", fontsize=24, fontweight="bold", color=COLORS["primary_green"], ha="center")
    ax.text(0, -0.3, "Active Slots", fontsize=10, color=COLORS["text_dim"], ha="center")
    ax.set_title("Snapshotter Slot Activity", fontsize=16, fontweight="bold", color=COLORS["primary_green"], pad=20)
    ax.set_ylim(0, 7)
    ax.set_aspect("equal")
    ax.axis("off")
    out_path = out_dir / "dsv_slot_distribution_radial.png"
    plt.tight_layout()
    plt.savefig(out_path, dpi=200, facecolor=COLORS["background"], bbox_inches="tight", pad_inches=0.3)
    plt.close()
    print(f"  Saved: {out_path.name}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate DSV production charts (matplotlib)")
    p.add_argument("--data-dir", type=Path, default=_PKG_DIR / "out", help="Primary export output directory")
    p.add_argument(
        "--merge-dir",
        type=Path,
        action="append",
        default=None,
        help="Export dir(s) to merge with --data-dir. Repeatable: e.g. --merge-dir out-days31-60 --merge-dir out-days61-90 for a 1-90 timeline.",
    )
    p.add_argument(
        "--summary-json",
        type=Path,
        default=None,
        help="Optional combined summary (default: merge participation_summary from both dirs)",
    )
    p.add_argument("--out-dir", type=Path, default=_PKG_DIR / "reports" / "charts", help="PNG output directory")
    p.add_argument("--day-start", type=int, default=1)
    p.add_argument("--day-end", type=int, default=None, help="Last protocol day in charts (default: max in health CSV)")
    p.add_argument("--min-active-days", type=int, default=None, help="Constellation filter (default: 22 for ≤30d span else 50)")
    p.add_argument("--tally-dir", type=Path, default=None, help="tally_reports/tallies for slot chart")
    p.add_argument("--tally-sample", type=int, default=500)
    p.add_argument("--skip-tally", action="store_true")
    p.add_argument(
        "--filename-suffix",
        default="",
        help="Append to PNG basenames (e.g. _days_31_60 → dsv_network_health_timeline_days_31_60.png)",
    )
    p.add_argument("--charts", nargs="*", default=["all"], help="Subset: timeline constellation latency matrix slots")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    data_dirs = [args.data_dir.resolve()]
    if args.merge_dir:
        data_dirs.extend(d.resolve() for d in args.merge_dir)

    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("DSV validator activity — chart generation")
    print("=" * 60)

    try:
        setup_neo_futuristic_style()
        validator_data = load_validator_data(data_dirs, args.summary_json)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    health = validator_data["health"]
    day_start = max(1, args.day_start)
    day_end = args.day_end if args.day_end is not None else int(health["day_id"].max())
    span = day_end - day_start + 1
    min_active = args.min_active_days if args.min_active_days is not None else (22 if span <= 30 else 50)
    title_suffix = f"Protocol Days {day_start}–{day_end}" if day_start > 1 or day_end != 30 else "First 30 Days of Production"

    charts = set(args.charts)
    if "all" in charts:
        charts = {"timeline", "constellation", "latency", "matrix", "slots"}

    suffix = args.filename_suffix or ""
    if suffix and not suffix.startswith("_"):
        suffix = f"_{suffix}"

    if "timeline" in charts:
        visualize_network_health_timeline(
            validator_data, out_dir, day_start, day_end, title_suffix, filename_suffix=suffix
        )
    if "constellation" in charts:
        visualize_validator_constellation(validator_data, out_dir, min_active, span)
    if "latency" in charts:
        visualize_latency_heatmap(validator_data, out_dir)
    if "matrix" in charts:
        visualize_epoch_coverage_matrix(validator_data, out_dir, day_start, day_end)

    if "slots" in charts and not args.skip_tally:
        tally_dir = args.tally_dir
        if tally_dir is None:
            candidate = _PKG_DIR.parent.parent / "tally_reports" / "tallies"
            tally_dir = candidate if candidate.is_dir() else None
        if tally_dir and tally_dir.is_dir():
            tally_data = load_tally_data(tally_dir.resolve(), args.tally_sample)
            visualize_slot_distribution_radial(tally_data, out_dir)
        else:
            print("  Skip slots: pass --tally-dir or place tallies at workspace/tally_reports/tallies")

    print("=" * 60)
    print(f"Charts written to: {out_dir}")
    print("=" * 60)


if __name__ == "__main__":
    main()
