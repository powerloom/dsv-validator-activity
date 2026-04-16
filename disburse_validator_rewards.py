#!/usr/bin/env python3
"""Disburse native L2 currency to validator reward recipients.

Reads <rewards-file> (produced by build_rewards.py), sends each `owner`
their `totalRewards` amount as native currency from a signer loaded from
the DISBURSER_PRIVATE_KEY env var, and appends one line per successful
transaction to an append-only JSONL ledger so the script is safely
re-runnable after a crash.

Required env vars:
  POWERLOOM_RPC_URL       RPC endpoint (same one used by other scripts)
  DISBURSER_PRIVATE_KEY   Hex private key of the funded signer
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from eth_account import Account
from web3 import Web3


DEFAULT_REWARDS_FILE = "out/validator_rewards.json"
DEFAULT_RECEIPTS_FILE = "out/disbursement_log.jsonl"
DEFAULT_GAS_LIMIT = 21000
TX_RECEIPT_TIMEOUT = 180


def _load_rewards(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise SystemExit(f"Missing rewards file: {path}")
    with path.open(encoding="utf-8") as f:
        rows = json.load(f)
    if not isinstance(rows, list) or not rows:
        raise SystemExit(f"{path} is empty or not a list")
    rows.sort(key=lambda r: int(r["id"]))
    return rows


def _load_already_sent(path: Path) -> set[tuple[int, str]]:
    sent: set[tuple[int, str]] = set()
    if not path.is_file():
        return sent
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if entry.get("status") == 1:
                sent.add((int(entry["id"]), Web3.to_checksum_address(entry["owner"])))
    return sent


def _to_wei(amount: float | int | str) -> int:
    return Web3.to_wei(Decimal(str(amount)), "ether")


def _fmt_native(wei: int) -> str:
    return f"{Decimal(wei) / Decimal(10**18):,.6f}"


def _append_receipt(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
        f.flush()
        os.fsync(f.fileno())


def main() -> None:
    parser = argparse.ArgumentParser(description="Disburse native L2 rewards")
    parser.add_argument("--rewards-file", default=DEFAULT_REWARDS_FILE)
    parser.add_argument("--receipts-file", default=DEFAULT_RECEIPTS_FILE)
    parser.add_argument("--dry-run", action="store_true", help="Plan only, send nothing")
    parser.add_argument("--yes", action="store_true", help="Skip interactive confirmation")
    parser.add_argument("--gas-limit", type=int, default=DEFAULT_GAS_LIMIT)
    args = parser.parse_args()

    rewards_path = Path(args.rewards_file)
    receipts_path = Path(args.receipts_file)

    rpc = os.environ.get("POWERLOOM_RPC_URL", "").strip()
    if not rpc:
        raise SystemExit("POWERLOOM_RPC_URL env var is required")

    pk = os.environ.get("DISBURSER_PRIVATE_KEY", "").strip()
    if not args.dry_run and not pk:
        raise SystemExit("DISBURSER_PRIVATE_KEY env var is required (unless --dry-run)")

    rows = _load_rewards(rewards_path)
    already_sent = _load_already_sent(receipts_path)

    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 180}))
    if not w3.is_connected():
        raise SystemExit(f"Could not connect to RPC: {rpc}")
    chain_id = int(w3.eth.chain_id)

    if pk:
        account = Account.from_key(pk)
        sender = Web3.to_checksum_address(account.address)
    else:
        account = None
        sender = None

    pending = []
    skipped = 0
    for row in rows:
        node_id = int(row["id"])
        owner = Web3.to_checksum_address(row["owner"])
        value_wei = _to_wei(row["totalRewards"])
        if (node_id, owner) in already_sent:
            skipped += 1
            continue
        pending.append({
            "id": node_id,
            "owner": owner,
            "totalRewards": row["totalRewards"],
            "value_wei": value_wei,
        })

    total_wei = sum(p["value_wei"] for p in pending)

    print(f"RPC:              {rpc}")
    print(f"Chain ID:         {chain_id}")
    print(f"Rewards file:     {rewards_path}")
    print(f"Receipts file:    {receipts_path}")
    print(f"Sender:           {sender if sender else '(dry-run, no key loaded)'}")
    if sender:
        sender_balance = w3.eth.get_balance(sender)
        print(f"Sender balance:   {_fmt_native(sender_balance)} native ({sender_balance} wei)")
    else:
        sender_balance = None
    print(f"Entries total:    {len(rows)}")
    print(f"Already sent:     {skipped}")
    print(f"Pending:          {len(pending)}")
    print()

    if not pending:
        print("Nothing to do — all entries already present in receipts log.")
        return

    print(f"{'id':>4}  {'owner':42}  {'totalRewards':>16}  {'value_wei':>32}")
    print("-" * 100)
    for p in pending:
        print(f"{p['id']:>4}  {p['owner']:42}  {p['totalRewards']:>16,.2f}  {p['value_wei']:>32}")
    print("-" * 100)
    print(f"{'TOTAL':>4}  {'':42}  {sum(p['totalRewards'] for p in pending):>16,.2f}  {total_wei:>32}")
    print(f"Grand total:      {_fmt_native(total_wei)} native ({total_wei} wei)")
    print()

    gas_price = w3.eth.gas_price
    gas_budget = gas_price * args.gas_limit * len(pending)
    print(f"Gas price:        {gas_price} wei")
    print(f"Gas budget:       {gas_budget} wei ({_fmt_native(gas_budget)} native) for {len(pending)} txs @ {args.gas_limit} gas")
    print()

    if sender_balance is not None:
        required = total_wei + gas_budget
        if sender_balance < required:
            short = required - sender_balance
            raise SystemExit(
                f"Insufficient balance: need {_fmt_native(required)} native, "
                f"have {_fmt_native(sender_balance)} (short by {_fmt_native(short)})"
            )

    if args.dry_run:
        print("Dry run — no transactions sent.")
        return

    if not args.yes:
        print("Type 'yes' to proceed and broadcast transactions:")
        try:
            answer = input("> ").strip().lower()
        except EOFError:
            answer = ""
        if answer != "yes":
            print("Aborted.")
            sys.exit(1)

    sent_count = 0
    spent_wei = 0
    try:
        for p in pending:
            nonce = w3.eth.get_transaction_count(sender, "pending")
            tx = {
                "to": p["owner"],
                "value": p["value_wei"],
                "gas": args.gas_limit,
                "gasPrice": w3.eth.gas_price,
                "nonce": nonce,
                "chainId": chain_id,
            }
            signed = account.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            tx_hash = w3.eth.send_raw_transaction(raw)
            tx_hash_hex = tx_hash.hex() if isinstance(tx_hash, bytes) else str(tx_hash)
            if not tx_hash_hex.startswith("0x"):
                tx_hash_hex = "0x" + tx_hash_hex
            print(f"[{p['id']:>3}] → {p['owner']}  {_fmt_native(p['value_wei']):>18} native  tx={tx_hash_hex}")

            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=TX_RECEIPT_TIMEOUT)
            status = int(receipt.get("status", 0))
            entry = {
                "id": p["id"],
                "owner": p["owner"],
                "amount_wei": str(p["value_wei"]),
                "tx_hash": tx_hash_hex,
                "block_number": int(receipt["blockNumber"]),
                "status": status,
                "chain_id": chain_id,
                "timestamp": int(time.time()),
            }
            _append_receipt(receipts_path, entry)

            if status != 1:
                raise SystemExit(f"Tx {tx_hash_hex} for id={p['id']} reverted (status={status}). Stopping.")

            sent_count += 1
            spent_wei += p["value_wei"]
            print(f"       confirmed block {entry['block_number']}")
    finally:
        remaining = len(pending) - sent_count
        print()
        print(f"Sent:      {sent_count}")
        print(f"Skipped:   {skipped} (already in receipts log)")
        print(f"Remaining: {remaining}")
        print(f"Spent:     {_fmt_native(spent_wei)} native ({spent_wei} wei)")


if __name__ == "__main__":
    main()
