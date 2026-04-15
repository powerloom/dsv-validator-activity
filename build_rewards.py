#!/usr/bin/env python3
"""Build per-node rewards from reliability_by_node_day.csv.

Splits a fixed daily pool evenly among the nodes active on each protocol day,
then resolves each node_id to its on-chain owner via
PowerloomValidatorState.nodeIdToOwner(uint256).

Reads from <data-dir>/reliability_by_node_day.csv (produced by
analyze_validator_reliability.py). Writes to <output-dir>/:
  - validator_rewards.json
  - validator_rewards.csv
  - daily_rewards.json
"""

import argparse
import csv
import json
import os
from pathlib import Path
from typing import Any

from web3 import Web3


# Self-contained: ABIs live next to this script under ./abi/
_PACKAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_VALIDATOR_STATE_ABI = _PACKAGE_DIR / "abi" / "PowerloomValidatorState.abi.json"

DEFAULT_VALIDATOR_STATE = "0x85573B2CF313315364FB4332f8eabc55321F201A"
DAILY_POOL = 50000.0
ZERO_ADDR = "0x" + "0" * 40


def _load_json(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing file: {path}. Keep abi/ next to this script when copying the tool."
        )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _read_node_day_csv(csv_path: Path) -> tuple[dict[int, set[int]], dict[int, set[int]]]:
    if not csv_path.is_file():
        raise SystemExit(
            f"Missing {csv_path}. Run analyze_validator_reliability.py first "
            "to produce reliability_by_node_day.csv."
        )
    node_days: dict[int, set[int]] = {}
    day_nodes: dict[int, set[int]] = {}
    with csv_path.open() as f:
        for row in csv.DictReader(f):
            node_id = int(row["node_id"])
            day_id = int(row["day_id"])
            node_days.setdefault(node_id, set()).add(day_id)
            day_nodes.setdefault(day_id, set()).add(node_id)
    if not node_days:
        raise SystemExit(f"{csv_path} has no rows — nothing to reward.")
    return node_days, day_nodes


def main() -> None:
    parser = argparse.ArgumentParser(description="Validator reward builder")
    parser.add_argument("--data-dir", default="out", help="Directory containing reliability_by_node_day.csv")
    parser.add_argument("--output-dir", default=None, help="Output directory (defaults to data-dir)")
    parser.add_argument(
        "--daily-pool",
        type=float,
        default=DAILY_POOL,
        help=f"Tokens distributed per active protocol day (default: {DAILY_POOL:g})",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    out_dir = Path(args.output_dir) if args.output_dir else data_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    rpc = os.environ.get("POWERLOOM_RPC_URL", "").strip()
    if not rpc:
        raise SystemExit("POWERLOOM_RPC_URL env var is required for build_rewards.py")

    vs_addr_raw = os.environ.get("VALIDATOR_STATE_CONTRACT", DEFAULT_VALIDATOR_STATE).strip()
    if not vs_addr_raw:
        raise SystemExit("VALIDATOR_STATE_CONTRACT env var is empty")
    vs_addr = Web3.to_checksum_address(vs_addr_raw)

    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 180}))
    vs_abi = _load_json(_DEFAULT_VALIDATOR_STATE_ABI)
    contract = w3.eth.contract(address=vs_addr, abi=vs_abi)

    csv_path = data_dir / "reliability_by_node_day.csv"
    node_days, day_nodes = _read_node_day_csv(csv_path)

    daily_pool = float(args.daily_pool)
    day_share = {day: daily_pool / len(nodes) for day, nodes in day_nodes.items()}

    rows = []
    for node_id in sorted(node_days):
        owner = contract.functions.nodeIdToOwner(node_id).call()
        if owner.lower() == ZERO_ADDR:
            raise RuntimeError(f"node {node_id}: owner is zero address")
        owner = Web3.to_checksum_address(owner)
        days = node_days[node_id]
        total = sum(day_share[d] for d in days)
        rows.append({
            "id": node_id,
            "owner": owner,
            "daysActive": len(days),
            "totalRewards": round(total, 2),
        })

    json_path = out_dir / "validator_rewards.json"
    csv_out_path = out_dir / "validator_rewards.csv"
    daily_path = out_dir / "daily_rewards.json"

    json_path.write_text(json.dumps(rows, indent=2) + "\n")

    with csv_out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "owner", "daysActive", "totalRewards"])
        writer.writeheader()
        writer.writerows(rows)

    daily = [
        {
            "day": day,
            "activeNodes": sorted(day_nodes[day]),
            "activeCount": len(day_nodes[day]),
            "rewardPerNode": round(day_share[day], 2),
        }
        for day in sorted(day_nodes)
    ]
    daily_path.write_text(json.dumps(daily, indent=2) + "\n")

    print(
        f"Wrote {json_path} ({len(rows)} nodes)\n"
        f"Wrote {csv_out_path}\n"
        f"Wrote {daily_path} ({len(daily)} days)"
    )


if __name__ == "__main__":
    main()
