#!/usr/bin/env python3
"""
Export DSV mainnet validator activity for data market days 1–30 using
DayStartedEvent L2 block boundaries (Powerloom L2 archive RPC required).

Supports long runs: checkpoint/resume, incremental JSONL, progress.json.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

try:
    from web3 import Web3
except ImportError:
    print("Install dependencies: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

# Self-contained: ABIs live next to this script under ./abi/
_PACKAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_PROTOCOL_ABI = _PACKAGE_DIR / "abi" / "PowerloomProtocolState.abi.json"
_DEFAULT_VPA_ABI = _PACKAGE_DIR / "abi" / "ValidatorPriorityAssigner.abi.json"
_DEFAULT_DM_ABI = _PACKAGE_DIR / "abi" / "DataMarket.abi.json"

DEFAULT_PROTOCOL_STATE = "0x1d0e010Ff11b781CA1dE34BD25a0037203e25E2a"
DEFAULT_DATA_MARKET = "0x26c44e5CcEB7Fe69Cffc933838CF40286b2dc01a"

DAY_START = 1
DAY_END = 30
CHUNK_BLOCKS = 4000
STATE_VERSION = 2
PROGRESS_EVERY_CHUNKS = 5
TX_CHECKPOINT_EVERY = 500
SIGNER_CHECKPOINT_EVERY = 50

_VALIDATOR_STATE_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "signerToNodeId",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

STATE_FILENAME = "export_state.json"
PROGRESS_FILENAME = "progress.json"


def _load_json(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing file: {path}. Keep abi/ next to this script when copying the tool."
        )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _atomic_write_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _build_day_intervals(
    day1_fallback_l2_block: int,
    day_started_blocks: dict[int, int],
    latest_block: int,
) -> dict[int, tuple[int, int]]:
    block_start: dict[int, int] = {}
    if 1 in day_started_blocks:
        block_start[1] = day_started_blocks[1]
    else:
        # On Arbitrum Nitro, DataMarket.deploymentBlockNumber uses Solidity block.number (parent-chain
        # style), not L2 eth_blockNumber — do not use it as an L2 block bound.
        block_start[1] = int(day1_fallback_l2_block)

    for d in range(2, 32):
        if d in day_started_blocks:
            block_start[d] = day_started_blocks[d]

    missing = [d for d in range(2, DAY_END + 1) if d not in block_start]
    if missing:
        raise SystemExit(
            f"Missing DataMarket DayStartedEvent for dayId(s): {missing}. "
            "Cannot bound days — check DATA_MARKET_CONTRACT, archive RPC, or incomplete discovery scan."
        )

    intervals: dict[int, tuple[int, int]] = {}
    for d in range(DAY_START, DAY_END + 1):
        start = block_start[d]
        nxt = d + 1
        if nxt in block_start:
            end = block_start[nxt] - 1
        else:
            end = int(latest_block)
        if end < start:
            raise SystemExit(f"Invalid range for day {d}: start={start} end={end}")
        intervals[d] = (start, end)
    return intervals


def _block_to_day(intervals: dict[int, tuple[int, int]], block_num: int) -> int | None:
    for d in range(DAY_START, DAY_END + 1):
        lo, hi = intervals[d]
        if lo <= block_num <= hi:
            return d
    return None


class ProgressReporter:
    """Writes progress.json and prints periodic status for long scans."""

    def __init__(self, out_dir: Path, phase: str, range_start: int, range_end: int) -> None:
        self.out_dir = out_dir
        self.phase = phase
        self.range_start = range_start
        self.range_end = range_end
        self.total_blocks = max(0, range_end - range_start + 1)
        self.t0 = time.monotonic()
        self.chunk_idx = 0
        self.last_block_done = range_start - 1

    def update(
        self,
        chunk_from: int,
        chunk_to: int,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.chunk_idx += 1
        self.last_block_done = chunk_to
        done_blocks = self.last_block_done - self.range_start + 1
        pct = (100.0 * done_blocks / self.total_blocks) if self.total_blocks else 100.0
        elapsed = time.monotonic() - self.t0
        rate = done_blocks / elapsed if elapsed > 0 else 0
        remaining = self.total_blocks - done_blocks
        eta_s = remaining / rate if rate > 0 else None

        row: dict[str, Any] = {
            "phase": self.phase,
            "range": [self.range_start, self.range_end],
            "last_block_completed": self.last_block_done,
            "blocks_done": done_blocks,
            "blocks_total": self.total_blocks,
            "pct_approx": round(pct, 2),
            "chunks": self.chunk_idx,
            "elapsed_sec": round(elapsed, 1),
            "blocks_per_sec_approx": round(rate, 1),
            "eta_sec_approx": round(eta_s, 0) if eta_s is not None and eta_s < 1e7 else None,
            "updated_unix": int(time.time()),
        }
        if extra:
            row.update(extra)
        _atomic_write_json(self.out_dir / PROGRESS_FILENAME, row)

        if self.chunk_idx % PROGRESS_EVERY_CHUNKS == 0 or chunk_to >= self.range_end:
            eta_h = eta_s / 3600 if eta_s else None
            eta_str = f" ETA~{eta_h:.1f}h" if eta_h is not None and eta_h < 168 else ""
            print(
                f"  [{self.phase}] blocks {chunk_from}-{chunk_to} | "
                f"{pct:.1f}% | {rate:.0f} blk/s{eta_str}",
                flush=True,
            )


def _fresh_cleanup(out_dir: Path) -> None:
    for name in (
        STATE_FILENAME,
        PROGRESS_FILENAME,
        "logs_priorities_assigned.jsonl",
        "logs_snapshot_batch_submitted.jsonl",
        "logs_batch_submissions_completed.jsonl",
        "tx_from_cache.jsonl",
        "signer_node_cache.jsonl",
    ):
        p = out_dir / name
        if p.exists():
            p.unlink()


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_state_merged(path: Path, patch: dict[str, Any]) -> None:
    """Merge patch into existing checkpoint so phases (e.g. discovery) are not dropped."""
    cur = _load_state(path) if path.exists() else {}
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(cur.get(k), dict):
            cur[k] = {**cur[k], **v}
        else:
            cur[k] = v
    _atomic_write_json(path, cur)


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                n += 1
    return n


def _jsonl_first_row_has_key(path: Path, key: str) -> bool:
    if not path.exists():
        return False
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                return key in json.loads(line)
    return False


def main() -> None:
    p = argparse.ArgumentParser(description="DSV validator activity export (days 1–30)")
    p.add_argument("--out", type=Path, default=Path("out"), help="Output directory")
    p.add_argument("--chunk-blocks", type=int, default=CHUNK_BLOCKS)
    p.add_argument(
        "--resume",
        action="store_true",
        help="Resume from export_state.json in --out (same RPC/contracts).",
    )
    p.add_argument(
        "--fresh",
        action="store_true",
        help="Delete checkpoint and partial JSONL in --out before starting.",
    )
    p.add_argument(
        "--discovery-from-block",
        type=int,
        default=None,
        metavar="N",
        help="L2 block to start DayStarted log scan (default: 1, or DISCOVERY_FROM_BLOCK env). "
        "Not DataMarket.deploymentBlockNumber() — that is parent-chain block style on Arbitrum Nitro.",
    )
    args = p.parse_args()
    chunk = max(500, args.chunk_blocks)

    rpc = os.environ.get("POWERLOOM_RPC_URL", "").strip()
    if not rpc:
        print("POWERLOOM_RPC_URL is required", file=sys.stderr)
        sys.exit(1)

    protocol_addr = os.environ.get("PROTOCOL_STATE_CONTRACT", DEFAULT_PROTOCOL_STATE).strip()
    data_market = os.environ.get("DATA_MARKET_CONTRACT", DEFAULT_DATA_MARKET).strip()

    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = out_dir / STATE_FILENAME

    if args.fresh:
        _fresh_cleanup(out_dir)
        print("Fresh run: removed checkpoint and partial outputs in", out_dir)

    if state_path.exists() and not args.resume and not args.fresh:
        print(
            f"{STATE_FILENAME} exists in {out_dir}. "
            "Use --resume to continue or --fresh to overwrite.",
            file=sys.stderr,
        )
        sys.exit(1)

    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 180}))
    if not w3.is_connected():
        print("Could not connect to POWERLOOM_RPC_URL", file=sys.stderr)
        sys.exit(1)

    chain_id = int(w3.eth.chain_id)
    env_cid = os.environ.get("CHAIN_ID", "").strip()
    if env_cid and int(env_cid) != chain_id:
        print(
            f"Warning: CHAIN_ID env {env_cid} != RPC chain_id {chain_id}",
            file=sys.stderr,
        )

    protocol_addr = w3.to_checksum_address(protocol_addr)
    data_market = w3.to_checksum_address(data_market)

    ps_abi = _load_json(_DEFAULT_PROTOCOL_ABI)
    protocol = w3.eth.contract(address=protocol_addr, abi=ps_abi)

    vpa_addr = protocol.functions.validatorPriorityAssigner().call()
    vs_addr = protocol.functions.validatorState().call()
    vpa_addr = w3.to_checksum_address(vpa_addr)
    vs_addr = w3.to_checksum_address(vs_addr)

    dm_abi = _load_json(_DEFAULT_DM_ABI)
    dm_contract = w3.eth.contract(address=data_market, abi=dm_abi)
    deployment_block = int(dm_contract.functions.deploymentBlockNumber().call())

    if args.discovery_from_block is not None:
        discovery_l2_start = max(1, int(args.discovery_from_block))
    else:
        raw = os.environ.get("DISCOVERY_FROM_BLOCK", "1").strip() or "1"
        discovery_l2_start = max(1, int(raw))

    vpa_abi = _load_json(_DEFAULT_VPA_ABI)
    vpa = w3.eth.contract(address=vpa_addr, abi=vpa_abi)
    vs_contract = w3.eth.contract(address=vs_addr, abi=_VALIDATOR_STATE_ABI)

    addresses = {
        "chain_id": chain_id,
        "protocol_state": protocol_addr,
        "data_market": data_market,
        "validator_priority_assigner": vpa_addr,
        "validator_state": vs_addr,
        "deployment_block_number": deployment_block,
        "deployment_block_number_note": (
            "Solidity block.number at DataMarket deploy (parent-chain style on Arbitrum Nitro; "
            "not comparable to eth_blockNumber or eth_getLogs block tags, which are L2 blocks)."
        ),
        "day_started_discovery_from_l2_block": discovery_l2_start,
    }
    _atomic_write_json(out_dir / "addresses_resolved.json", addresses)

    state = _load_state(state_path) if args.resume and state_path.exists() else {}

    if state and state.get("phase") == "done" and args.resume:
        print(
            "Export already complete (phase=done). See metrics_summary.json. "
            "Use --fresh to re-run from scratch."
        )
        sys.exit(0)

    if state:
        if state.get("version") != STATE_VERSION:
            print("Checkpoint version mismatch; use --fresh or new --out", file=sys.stderr)
            sys.exit(1)
        if int(state.get("chain_id", 0)) != chain_id:
            print("CHAIN_ID mismatch vs checkpoint", file=sys.stderr)
            sys.exit(1)
        if state.get("protocol_state", "").lower() != protocol_addr.lower():
            print("PROTOCOL_STATE mismatch vs checkpoint", file=sys.stderr)
            sys.exit(1)
        if state.get("data_market", "").lower() != data_market.lower():
            print("DATA_MARKET mismatch vs checkpoint", file=sys.stderr)
            sys.exit(1)
        print(f"Resuming from phase={state.get('phase')} checkpoint")

    latest = int(w3.eth.block_number)

    # --- Phase: discover DayStarted on DataMarket (checkpointed) ---
    # Canonical emit: DataMarket.sol — DayStartedEvent(uint256 dayId, uint256 timestamp).
    # ProtocolState also emits a mirror when releaseEpoch/forceSkipEpoch go through it; watcher
    # tooling often indexes the DataMarket address — use the same source here.
    # eth_getLogs uses L2 block numbers. DataMarket.deploymentBlockNumber() is not L2 (Arbitrum
    # Nitro: Solidity block.number tracks parent chain). Scan from discovery_l2_start (default 1).
    day_started_blocks: dict[int, int] = {}
    discover_key = "discover_day_started"
    dstate = state.get(discover_key, {}) if state else {}

    if dstate.get("complete"):
        day_started_blocks = {int(k): int(v) for k, v in dstate["day_started_first_block"].items()}
        print("Skipping DayStarted discovery (checkpoint complete)")
    else:
        scan_to = latest
        day_started_blocks = {
            int(k): int(v) for k, v in dstate.get("day_started_first_block", {}).items()
        }
        start_fb = int(dstate.get("last_scanned_block", discovery_l2_start - 1)) + 1
        if start_fb > scan_to:
            raise SystemExit(
                f"Invalid discovery range: start_block={start_fb} > end_block={scan_to}. "
                "Checkpoint may be from another chain — run with --fresh, or fix RPC / addresses."
            )
        prog = ProgressReporter(out_dir, "discover_DataMarket_DayStartedEvent", start_fb, scan_to)
        print(
            f"DayStarted discovery (DataMarket {data_market}): L2 blocks {start_fb}..{scan_to} "
            f"(from discovery_l2_start={discovery_l2_start}; may be millions of blocks; checkpointed)"
        )
        fb = start_fb
        while fb <= scan_to:
            tb = min(fb + chunk - 1, scan_to)
            entries = dm_contract.events.DayStartedEvent.get_logs(
                from_block=fb,
                to_block=tb,
            )
            for entry in entries:
                args = entry["args"]
                day_id = int(args["dayId"])
                bn = int(entry["blockNumber"])
                if day_id not in day_started_blocks:
                    day_started_blocks[day_id] = bn
            _save_state_merged(
                state_path,
                {
                    "version": STATE_VERSION,
                    "chain_id": chain_id,
                    "protocol_state": protocol_addr,
                    "data_market": data_market,
                    "phase": discover_key,
                    discover_key: {
                        "complete": False,
                        "last_scanned_block": tb,
                        "day_started_first_block": {
                            str(k): v for k, v in sorted(day_started_blocks.items())
                        },
                    },
                },
            )
            prog.update(fb, tb, {"events_found_so_far": len(day_started_blocks)})
            fb = tb + 1
            time.sleep(0.05)

        _save_state_merged(
            state_path,
            {
                "version": STATE_VERSION,
                "chain_id": chain_id,
                "protocol_state": protocol_addr,
                "data_market": data_market,
                "phase": "intervals_ready",
                discover_key: {
                    "complete": True,
                    "last_scanned_block": scan_to,
                    "day_started_first_block": {
                        str(k): v for k, v in sorted(day_started_blocks.items())
                    },
                },
            },
        )

    intervals = _build_day_intervals(1, day_started_blocks, latest)

    boundaries = {
        str(d): {"block_start": intervals[d][0], "block_end": intervals[d][1]}
        for d in range(DAY_START, DAY_END + 1)
    }
    _atomic_write_json(
        out_dir / "day_boundaries.json",
        {
            "day_started_event_first_block_by_day": {str(k): v for k, v in sorted(day_started_blocks.items())},
            "intervals_inclusive_days_1_30": boundaries,
            "note": (
                "Day boundaries from DataMarket DayStartedEvent (L2 block numbers). "
                "If dayId=1 event missing, day 1 starts at L2 block 1 (not deploymentBlockNumber(), "
                "which is parent-chain style on Arbitrum Nitro)."
            ),
        },
    )

    global_from = intervals[DAY_START][0]
    global_to = intervals[DAY_END][1]
    print(f"Export window: blocks {global_from} .. {global_to} (chain_id={chain_id})")

    def run_chunked_logs(
        phase_name: str,
        file_key: str,
        rel_path: str,
        fetch_logs: Callable[..., Any],
        map_row: Callable[[Any, dict[int, tuple[int, int]]], dict[str, Any]],
    ) -> int:
        """Scan global_from..global_to with resume; append JSONL. Returns line count (streaming; no full list)."""
        path = out_dir / rel_path
        cur_state = _load_state(state_path)
        st = cur_state.get(file_key, {})
        last_done = int(st.get("last_block_done", global_from - 1))
        complete = bool(st.get("complete"))
        rows_written = 0

        if complete and path.exists():
            n = _count_jsonl(path)
            print(f"Skipping {phase_name} (checkpoint complete); {rel_path} has {n} lines")
            return n

        start_fb = last_done + 1
        if start_fb > global_to:
            return 0

        mode = "a" if last_done >= global_from and path.exists() else "w"
        prog = ProgressReporter(out_dir, phase_name, start_fb, global_to)
        print(f"{phase_name}: blocks {start_fb}..{global_to}")

        fb = start_fb
        while fb <= global_to:
            tb = min(fb + chunk - 1, global_to)
            logs = fetch_logs(fb, tb)
            with open(path, mode, encoding="utf-8") as f:
                for log in logs:
                    row = map_row(log, intervals)
                    f.write(json.dumps(row, default=str) + "\n")
                    rows_written += 1
                f.flush()
                os.fsync(f.fileno())
            mode = "a"

            _save_state_merged(
                state_path,
                {
                    "version": STATE_VERSION,
                    "chain_id": chain_id,
                    "protocol_state": protocol_addr,
                    "data_market": data_market,
                    "phase": file_key,
                    file_key: {
                        "complete": False,
                        "last_block_done": tb,
                        "lines_written": rows_written,
                    },
                },
            )
            prog.update(fb, tb, {"lines_in_phase": rows_written})
            fb = tb + 1
            time.sleep(0.05)

        _save_state_merged(
            state_path,
            {
                "version": STATE_VERSION,
                "chain_id": chain_id,
                "protocol_state": protocol_addr,
                "data_market": data_market,
                "phase": file_key + "_done",
                file_key: {
                    "complete": True,
                    "last_block_done": global_to,
                    "lines_written": rows_written,
                },
            },
        )
        return rows_written

    # Reload state after discovery mutations
    state = _load_state(state_path)

    run_chunked_logs(
        "PrioritiesAssigned (VPA)",
        "priorities",
        "logs_priorities_assigned.jsonl",
        lambda fb, tb: vpa.events.PrioritiesAssigned.get_logs(
            from_block=fb,
            to_block=tb,
            argument_filters={"dataMarket": data_market},
        ),
        lambda log, iv: (
            lambda a, bn: {
                "event": "PrioritiesAssigned",
                "block_number": bn,
                "tx_hash": log["transactionHash"].hex(),
                "log_index": int(log["logIndex"]),
                "day_id": _block_to_day(iv, bn),
                "epoch_id": int(a["epochId"]),
                "validator_count": int(a["validatorCount"]),
                "seed": str(a["seed"]),
                "timestamp": int(a["timestamp"]),
            }
        )(log["args"], int(log["blockNumber"])),
    )
    state = _load_state(state_path)

    run_chunked_logs(
        "SnapshotBatchSubmitted",
        "snapshot_submitted",
        "logs_snapshot_batch_submitted.jsonl",
        lambda fb, tb: protocol.events.SnapshotBatchSubmitted.get_logs(
            from_block=fb,
            to_block=tb,
            argument_filters={"dataMarketAddress": data_market},
        ),
        lambda log, iv: (
            lambda a, bn: {
                "event": "SnapshotBatchSubmitted",
                "block_number": bn,
                "tx_hash": log["transactionHash"].hex(),
                "log_index": int(log["logIndex"]),
                "day_id": _block_to_day(iv, bn),
                "epoch_id": int(a["epochId"]),
                "batch_cid": a["batchCid"],
                "timestamp": int(a["timestamp"]),
            }
        )(log["args"], int(log["blockNumber"])),
    )
    state = _load_state(state_path)

    run_chunked_logs(
        "BatchSubmissionsCompleted",
        "batch_completed",
        "logs_batch_submissions_completed.jsonl",
        lambda fb, tb: protocol.events.BatchSubmissionsCompleted.get_logs(
            from_block=fb,
            to_block=tb,
            argument_filters={"dataMarketAddress": data_market},
        ),
        lambda log, iv: (
            lambda a, bn: {
                "event": "BatchSubmissionsCompleted",
                "block_number": bn,
                "tx_hash": log["transactionHash"].hex(),
                "log_index": int(log["logIndex"]),
                "day_id": _block_to_day(iv, bn),
                "epoch_id": int(a["epochId"]),
                "timestamp": int(a["timestamp"]),
            }
        )(log["args"], int(log["blockNumber"])),
    )
    state = _load_state(state_path)

    sub_path = out_dir / "logs_snapshot_batch_submitted.jsonl"
    end_path = out_dir / "logs_batch_submissions_completed.jsonl"
    prio_path = out_dir / "logs_priorities_assigned.jsonl"

    # --- Transactions: fetch tx.from with cache + checkpoint ---
    tx_cache_path = out_dir / "tx_from_cache.jsonl"
    tx_from: dict[str, str] = {}
    if tx_cache_path.exists():
        with open(tx_cache_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    o = json.loads(line)
                    tx_from[o["tx_hash"]] = o["from"]

    th: set[str] = set()
    for row in _iter_jsonl(sub_path):
        th.add(row["tx_hash"])
    for row in _iter_jsonl(end_path):
        th.add(row["tx_hash"])
    tx_hashes = sorted(th)

    tx_state = state.get("transactions", {}) if state else {}
    start_idx = int(tx_state.get("next_index", 0))
    tx_complete = bool(tx_state.get("complete"))
    if start_idx > 0 and not tx_complete:
        print(f"Resuming tx fetch from index {start_idx}/{len(tx_hashes)}")

    if tx_complete:
        print(
            f"Skipping eth_getTransaction (checkpoint: {len(tx_from)} txs in tx_from_cache.jsonl)"
        )
    else:
        print(
            f"Fetching tx.from for up to {len(tx_hashes)} unique transactions "
            f"(cached: {len(tx_from)})..."
        )
        for i in range(start_idx, len(tx_hashes)):
            h = tx_hashes[i]
            if h in tx_from:
                continue
            tx = w3.eth.get_transaction(h)
            addr = tx["from"]
            tx_from[h] = addr
            with open(tx_cache_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"tx_hash": h, "from": addr}) + "\n")
                f.flush()
                os.fsync(f.fileno())
            if (i + 1) % TX_CHECKPOINT_EVERY == 0 or i + 1 == len(tx_hashes):
                _save_state_merged(
                    state_path,
                    {
                        "version": STATE_VERSION,
                        "chain_id": chain_id,
                        "protocol_state": protocol_addr,
                        "data_market": data_market,
                        "phase": "transactions",
                        "transactions": {"next_index": i + 1, "total": len(tx_hashes)},
                    },
                )
                print(f"  tx checkpoint {i + 1}/{len(tx_hashes)}", flush=True)

    def _stream_enrich_jsonl(src: Path, dst: Path) -> None:
        with open(src, encoding="utf-8") as f_in, open(dst, "w", encoding="utf-8") as f_out:
            for line in f_in:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                row["signer_from"] = tx_from.get(row["tx_hash"], "")
                f_out.write(json.dumps(row, default=str) + "\n")

    tmp_sub = out_dir / "logs_snapshot_batch_submitted.jsonl.tmp"
    tmp_end = out_dir / "logs_batch_submissions_completed.jsonl.tmp"
    if _jsonl_first_row_has_key(sub_path, "signer_from") and _jsonl_first_row_has_key(
        end_path, "signer_from"
    ):
        print("Skipping JSONL enrich (signer_from already present on first row)")
    else:
        _stream_enrich_jsonl(sub_path, tmp_sub)
        _stream_enrich_jsonl(end_path, tmp_end)
        tmp_sub.replace(sub_path)
        tmp_end.replace(end_path)

    _save_state_merged(
        state_path,
        {
            "version": STATE_VERSION,
            "chain_id": chain_id,
            "protocol_state": protocol_addr,
            "data_market": data_market,
            "transactions": {"next_index": len(tx_hashes), "total": len(tx_hashes), "complete": True},
            "phase": "signers",
        },
    )

    signers: set[str] = set()
    for row in _iter_jsonl(sub_path):
        sf = row.get("signer_from")
        if sf:
            signers.add(sf)
    for row in _iter_jsonl(end_path):
        sf = row.get("signer_from")
        if sf:
            signers.add(sf)

    signer_cache_path = out_dir / "signer_node_cache.jsonl"
    signer_node: dict[str, int] = {}
    if signer_cache_path.exists():
        with open(signer_cache_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    o = json.loads(line)
                    signer_node[o["signer"]] = int(o["node_id"])

    sorted_signers = sorted(signers)
    print(f"signerToNodeId for {len(sorted_signers)} signers (cached {len(signer_node)})...")
    for idx, s in enumerate(sorted_signers):
        if s in signer_node:
            continue
        nid = int(vs_contract.functions.signerToNodeId(w3.to_checksum_address(s)).call())
        signer_node[s] = nid
        with open(signer_cache_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"signer": s, "node_id": nid}) + "\n")
            f.flush()
            os.fsync(f.fileno())
        if (idx + 1) % SIGNER_CHECKPOINT_EVERY == 0:
            _save_state_merged(
                state_path,
                {
                    "version": STATE_VERSION,
                    "chain_id": chain_id,
                    "protocol_state": protocol_addr,
                    "data_market": data_market,
                    "phase": "signers",
                    "signers": {"resolved": idx + 1, "total": len(sorted_signers)},
                },
            )
            print(f"  signer checkpoint {idx + 1}/{len(sorted_signers)}", flush=True)

    # Metrics (streaming)
    metrics: dict[tuple[str, int], dict[str, int]] = defaultdict(
        lambda: {
            "snapshot_batch_submitted": 0,
            "batch_submissions_completed": 0,
        }
    )

    for row in _iter_jsonl(sub_path):
        d = row.get("day_id")
        sf = row.get("signer_from")
        if d is None or not sf:
            continue
        metrics[(sf, int(d))]["snapshot_batch_submitted"] += 1

    for row in _iter_jsonl(end_path):
        d = row.get("day_id")
        sf = row.get("signer_from")
        if d is None or not sf:
            continue
        metrics[(sf, int(d))]["batch_submissions_completed"] += 1

    prio_by_day: dict[int, int] = defaultdict(int)
    for row in _iter_jsonl(prio_path):
        d = row.get("day_id")
        if d is not None:
            prio_by_day[int(d)] += 1

    n_sub = _count_jsonl(sub_path)
    n_end = _count_jsonl(end_path)
    n_prio = _count_jsonl(prio_path)

    csv_path = out_dir / "metrics_by_signer_day.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as cf:
        w = csv.writer(cf)
        w.writerow(
            [
                "signer",
                "node_id",
                "day_id",
                "snapshot_batch_submitted",
                "batch_submissions_completed",
            ]
        )
        keys = sorted(
            ((s, d) for (s, d) in metrics),
            key=lambda x: (x[1], x[0]),
        )
        for signer, day in keys:
            nid = signer_node.get(signer, 0)
            m = metrics[(signer, day)]
            w.writerow(
                [
                    signer,
                    nid,
                    day,
                    m["snapshot_batch_submitted"],
                    m["batch_submissions_completed"],
                ]
            )

    summary = {
        "days": list(range(DAY_START, DAY_END + 1)),
        "priorities_assigned_logs_per_day": {str(k): v for k, v in sorted(prio_by_day.items())},
        "unique_signers_observed": len(signers),
        "totals": {
            "snapshot_batch_submitted": n_sub,
            "batch_submissions_completed": n_end,
            "priorities_assigned": n_prio,
        },
    }
    _atomic_write_json(out_dir / "metrics_summary.json", summary)

    final_state = _load_state(state_path)
    final_state.update(
        {
            "version": STATE_VERSION,
            "chain_id": chain_id,
            "protocol_state": protocol_addr,
            "data_market": data_market,
            "phase": "done",
            "completed_unix": int(time.time()),
        }
    )
    _atomic_write_json(state_path, final_state)
    _atomic_write_json(
        out_dir / PROGRESS_FILENAME,
        {
            "phase": "done",
            "message": "export complete",
            "updated_unix": int(time.time()),
        },
    )

    print(f"Done. Outputs in {out_dir.resolve()}")


if __name__ == "__main__":
    main()
