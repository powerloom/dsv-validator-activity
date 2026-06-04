# DSV mainnet: validator activity export (configurable day range)

Exports on-chain validator activity from Powerloom L2 using **`DataMarket`** `DayStartedEvent` block boundaries (see workspace plan). Day discovery does **not** use `ProtocolState` for `DayStartedEvent` — the canonical emit for epoch/day advances is on the **data market** contract (watchers and on-chain reward flows index that address).

**Self-contained:** This directory is enough to run the tool: `export_validator_activity.py`, `requirements.txt`, and **`abi/`** with normal **JSON ABI files** (a single JSON **array** of ABI entries — the same shape `web3.eth.contract(..., abi=...)` expects). No dependency on `decentralized-sequencer/` or a fixed monorepo path. Copy the whole `dsv-validator-activity/` folder elsewhere and run with `pip install -r requirements.txt` plus `POWERLOOM_RPC_URL`.

**web3.py:** `requirements.txt` allows **web3 6.x or 7.x**. Event `get_logs` uses different keyword names between versions; the script wraps both (`from_block` vs `fromBlock`).

### `abi/` JSON files (all are plain ABI arrays)

| File | Used for |
|------|----------|
| **`abi/DataMarket.abi.json`** | **DataMarket** — **`DayStartedEvent`** for day boundary discovery (L2 `eth_getLogs`). `deploymentBlockNumber()` is stored for reference only — on Arbitrum Nitro it is **not** an L2 block (see `deployment_block_number_note` in `addresses_resolved.json`). |
| **`abi/PowerloomProtocolState.abi.json`** | **ProtocolState** — `SnapshotBatchSubmitted`, `BatchSubmissionsCompleted`, and `validatorPriorityAssigner()` / `validatorState()` calls. (ProtocolState may also emit a mirror `DayStartedEvent` in some code paths; this tool does not rely on that.) |
| **`abi/ValidatorPriorityAssigner.abi.json`** | **ValidatorPriorityAssigner (VPA)** — `PrioritiesAssigned` logs. Same format: one JSON array. |
| **`abi/PowerloomValidatorState.abi.json`** | **ValidatorState** — `nodeIdToOwner(uint256)` for reward owner resolution in `build_rewards.py`. |

**Inline only:** **ValidatorState** (`signerToNodeId`) uses a **minimal ABI** in `export_validator_activity.py` — only that function is needed.

### Published reports (committed)

Verified analysis JSON (no raw JSONL dumps) lives under **[`reports/`](reports/)** — days 1–30, days 31–60, and a combined 60-day summary. See [`reports/README.md`](reports/README.md) for layout and reproduction steps.

### Charts (matplotlib)

Neo-futuristic PNGs for docs/blog live under **`reports/charts/`** (optional commit). Generator: **`generate_visualizations.py`**.

```bash
pip install -r requirements-viz.txt

# After export + analyzers on ./out (days 1–30)
python analyze_network_health.py --data-dir out
python analyze_multikey_nodes.py --data-dir out
python analyze_epoch_participation.py --data-dir out
python generate_visualizations.py --data-dir out --day-end 30

# 60-day timeline (merge two export dirs; JSONL stays gitignored)
python generate_visualizations.py \
  --data-dir out \
  --merge-dir ./out-days31-60 \
  --day-start 1 --day-end 60 \
  --summary-json reports/combined-days-1-60/summary.json \
  --charts timeline constellation latency matrix

# 90-day timeline (--merge-dir is repeatable across windows)
python generate_visualizations.py \
  --data-dir out \
  --merge-dir ./out-days31-60 \
  --merge-dir ./out-days61-90 \
  --day-start 1 --day-end 90 \
  --summary-json reports/combined-days-1-90/summary.json \
  --charts timeline constellation latency matrix
```



## Environment

| Variable | Description |
|----------|-------------|
| `POWERLOOM_RPC_URL` | **Required.** Archive-capable JSON-RPC for Powerloom mainnet (Arbitrum Nitro L2). |
| `PROTOCOL_STATE_CONTRACT` | Defaults to `0x1d0e010Ff11b781CA1dE34BD25a0037203e25E2a` (see `localenvs/dsv-mainnet`). |
| `DATA_MARKET_CONTRACT` | Defaults to `0x26c44e5CcEB7Fe69Cffc933838CF40286b2dc01a`. |
| `VALIDATOR_STATE_CONTRACT` | Defaults to `0x85573B2CF313315364FB4332f8eabc55321F201A`. Used by `build_rewards.py` to resolve `nodeIdToOwner(uint256)`. |
| `DISBURSER_PRIVATE_KEY` | Required by `disburse_validator_rewards.py` (unless `--dry-run`). Hex private key of the funded signer that pays out `validator_rewards.json`. Never logged. |
| `CHAIN_ID` | Optional; if set, compared to `eth_chainId` (sanity check). |
| `DISCOVERY_FROM_BLOCK` | Optional L2 block to begin **`DayStartedEvent`** log scan (default **`1`**). **Do not** set this from `DataMarket.deploymentBlockNumber()` — on **Arbitrum Nitro** that value follows Solidity `block.number` (parent-chain style) and is **not** the same coordinate system as `eth_blockNumber` / `eth_getLogs`. CLI: `--discovery-from-block N` overrides the env. |

## Usage

```bash
cd scripts/dsv-validator-activity
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export POWERLOOM_RPC_URL=https://...
python export_validator_activity.py --out ./out
```

**Day 31–60 window** (after the initial days 1–30 export):

```bash
# Use a separate output dir; optional: set discovery near day 30 end from out/day_boundaries.json
python export_validator_activity.py --out ./out-days31-60 --day-start 31 --day-end 60 --fresh
python analyze_epoch_participation.py --data-dir out-days31-60
python analyze_validator_reliability.py --data-dir out-days31-60
python compare_windows.py --baseline-dir out --compare-dir out-days31-60
```

**Day 61–90 window** (after days 31–60). Seed discovery near the prior day-60 boundary to skip re-scanning early L2 blocks (read `block_end` for day 60 from the 31–60 `day_boundaries.json`):

```bash
python export_validator_activity.py --out ./out-days61-90 --day-start 61 --day-end 90 \
  --discovery-from-block <day60_block_end+1> --fresh
python analyze_epoch_participation.py --data-dir out-days61-90
python analyze_validator_reliability.py --data-dir out-days61-90
python compare_windows.py --baseline-dir out-days31-60 --compare-dir out-days61-90
```

Boundaries land under `intervals_inclusive_days_61_90` in `day_boundaries.json`.

**First run** uses an empty `--out` (no `export_state.json`). If a previous run left a checkpoint there, you must pass **`--resume`** to continue or **`--fresh`** to delete checkpoint + partial outputs and start over.

```bash
python export_validator_activity.py --out ./out --resume
python export_validator_activity.py --out ./out --fresh
```

All artifacts are written under **`--out`** (default `./out`). See [Output files and formats](#output-files-and-formats) and [Using outputs for manual rewards](#using-outputs-for-manual-rewards).

## Output files and formats

### Primary artifacts (reward attribution and audit)

| File | Format | Contents |
|------|--------|----------|
| **`addresses_resolved.json`** | JSON | `chain_id`, `protocol_state`, `data_market`, `validator_priority_assigner`, `validator_state`, `deployment_block_number`. Use for reproducibility and verifying you scanned the intended deployment. |
| **`day_boundaries.json`** | JSON | `day_started_event_first_block_by_day`: first L2 block seen per `dayId` from **DataMarket** `DayStartedEvent`. `intervals_inclusive_days_1_30`: for each protocol **day 1–30**, inclusive **`block_start`** and **`block_end`** on Powerloom L2. Days follow those boundaries (not fixed epoch counts per day). |
| **`metrics_by_signer_day.csv`** | CSV | **Main table for manual reward spreadsheets.** Columns: **`signer`**, **`node_id`** (from `ValidatorState.signerToNodeId`), **`day_id`**, **`snapshot_batch_submitted`**, **`batch_submissions_completed`**. One row per distinct **(signer, day_id)** with activity. |
| **`metrics_summary.json`** | JSON | `priorities_assigned_logs_per_day` (counts of VPA `PrioritiesAssigned` logs per day), `unique_signers_observed`, `totals` for the three submission-related log types. Sanity check against JSONL line counts. |

### Raw event logs (one JSON object per line)

| File | Format | Contents |
|------|--------|----------|
| **`logs_priorities_assigned.jsonl`** | JSONL | VPA **`PrioritiesAssigned`**: `epoch_id`, `validator_count`, `seed`, `timestamp`, `block_number`, `tx_hash`, optional `day_id`. **No signer** — shows epoch-level priority rotation for the data market in each day’s L2 block range. |
| **`logs_snapshot_batch_submitted.jsonl`** | JSONL | ProtocolState **`SnapshotBatchSubmitted`**: `batch_cid`, `epoch_id`, `timestamp`, `block_number`, `tx_hash`, `day_id`, **`signer_from`** (transaction sender). |
| **`logs_batch_submissions_completed.jsonl`** | JSONL | ProtocolState **`BatchSubmissionsCompleted`**: `epoch_id`, `timestamp`, `block_number`, `tx_hash`, `day_id`, **`signer_from`**. |

### Run control / resume (not used for reward math)

| File | Purpose |
|------|---------|
| **`export_state.json`** | Checkpoint for `--resume` (phase, per-stage block progress, tx index, etc.). |
| **`progress.json`** | Last-known scan progress (phase, block range, approximate %, rate). |
| **`tx_from_cache.jsonl`** | Cached `tx_hash` → `from` for faster resume. |
| **`signer_node_cache.jsonl`** | Cached `signer` → `node_id` for faster resume. |

### On-chain meaning of key fields

- **`signer_from`**: Address that sent the transaction (`eth_getTransaction.from`). For these flows it is the **authorized signer** for validator submissions as enforced by **`ValidatorPriorityAssigner.canValidatorSubmit`** and **`ValidatorState.signerToNodeId`**.
- **`node_id`**: Output of **`signerToNodeId(signer)`** on the resolved ValidatorState contract — links the signing key to a **validator node** for operator-level reporting.
- **`day_id`**: Protocol **data market day** (1–30), assigned by **L2 block number** falling in the inclusive range for that day from **`day_boundaries.json`** (built from **DataMarket** `DayStartedEvent`). Handles irregular epoch counts (e.g. short day 1, `forceSkipEpoch` windows).

### What this export does *not* include

- **Snapshotter / slot daily rewards** from **`RewardsDistributedEvent`** and related **`ProtocolState` slot accounting** are a **separate** on-chain path. This tool is aimed at **DSV validator submission activity** (VPA + batch txs), not lite-node snapshotter reward distribution.

---

## Using outputs for manual rewards

The script **does not** compute token amounts or a payout formula. It produces **evidence and counts** so operators can apply **their own** policy (weights, caps, minimums, exclusions).

1. **Start from `metrics_by_signer_day.csv`**  
   Aggregate or weight **`snapshot_batch_submitted`** and **`batch_submissions_completed`** per **`node_id`** and/or **`signer`**, per **`day_id`** or across the full window. Join **`node_id`** to your off-chain roster (operator name, payout address, etc.).

2. **Use JSONL for audits and disputes**  
   Trace any row back to **`tx_hash`**, **`block_number`**, and **`day_id`**. Compare with **`logs_priorities_assigned.jsonl`** for epoch-level VPA context in the same block ranges (no per-signer breakdown there).

3. **Sanity checks**  
   - Compare **`metrics_summary.json`** totals to line counts in the JSONL files.  
   - Cross-check known **`VPA_SIGNER_ADDRESSES`** from [`localenvs/dsv-mainnet`](../../localenvs/dsv-mainnet) envs against **`signer`** / **`signer_from`** in outputs.

4. **Define payout rules off-chain**  
   Examples: proportional to submits, separate weights for submit vs. end-batch, daily caps, inactive-day rules — all applied **outside** this script using CSV/JSONL as inputs.

### Example payout: `build_rewards.py`

`build_rewards.py` is a **sample policy** on top of the reliability table: it splits a fixed daily pool (default **50,000**) evenly among the nodes that were active on each protocol day, then resolves each `node_id` to its on-chain owner via `PowerloomValidatorState.nodeIdToOwner(uint256)`. It is an example — not the canonical reward formula — and requires `analyze_validator_reliability.py` to have produced `out/reliability_by_node_day.csv` first.

```bash
export POWERLOOM_RPC_URL=https://...
python export_validator_activity.py --out ./out
python analyze_validator_reliability.py --data-dir out
python build_rewards.py --data-dir out
```

Override the daily pool with `--daily-pool 42000`, or point at a different validator-state contract with `VALIDATOR_STATE_CONTRACT=0x...`.

| File | Format | Contents |
|------|--------|----------|
| **`validator_rewards.json`** | JSON | `[{id, owner, daysActive, totalRewards}]` — one row per node, owner resolved on-chain. |
| **`validator_rewards.csv`** | CSV | Same columns as the JSON, ready to paste into a spreadsheet. |
| **`daily_rewards.json`** | JSON | `[{day, activeNodes, activeCount, rewardPerNode}]` — one row per protocol day, showing the even-split share for that day. |

### Paying out: `disburse_validator_rewards.py`

`disburse_validator_rewards.py` takes `validator_rewards.json` and sends each `owner` their `totalRewards` as **native currency** on Powerloom L2 from a signer loaded from `DISBURSER_PRIVATE_KEY`. It is a one-off payout tool — no distributor contract, no recurring job. `totalRewards` is interpreted as whole native units (1 token = 10^18 wei), converted via `Decimal` → `Web3.to_wei('ether')` to avoid float drift.

```bash
export POWERLOOM_RPC_URL=https://...
python disburse_validator_rewards.py --dry-run                     # plan only, no tx
export DISBURSER_PRIVATE_KEY=0x...                                 # funded signer
python disburse_validator_rewards.py                               # interactive confirm, then broadcast
```

CLI flags: `--rewards-file` (default `out/validator_rewards.json`), `--receipts-file` (default `out/disbursement_log.jsonl`), `--dry-run`, `--yes` (skip the interactive confirmation), `--gas-limit` (default `21000`).

Safety behaviors:

- **Pre-flight** prints sender, balance, chain id, per-row `value_wei`, grand total, and gas budget. Aborts if `balance < total + gas_budget`.
- **Interactive confirmation** requires typing `yes` (bypass with `--yes`, or skip entirely with `--dry-run`).
- **Idempotent re-run:** every confirmed tx is appended to `disbursement_log.jsonl` with `fsync` immediately after receipt. Re-running skips any `(id, owner)` already logged with `status: 1` — safely resumable after a crash or partial run.
- **Legacy tx** (`gasPrice = eth.gas_price`, `gas = 21000`), fresh `pending` nonce per iteration, `chainId` from `eth_chainId`.
- **No auto-retry.** Stops immediately on revert, receipt timeout (180s), or any RPC error and prints a done-vs-remaining summary.

| File | Format | Contents |
|------|--------|----------|
| **`disbursement_log.jsonl`** | JSONL | Append-only idempotency ledger. One line per confirmed payout: `{id, owner, amount_wei, tx_hash, block_number, status, chain_id, timestamp}`. |

---

## Long runs (millions of L2 blocks)

Scans are **chunked** (default 4000 blocks per `eth_getLogs`; tune with `--chunk-blocks`). The script is built for **hours-long** archive RPC pulls:

| Mechanism | Purpose |
|-----------|---------|
| **`export_state.json`** | Merged checkpoint: phase, `last_scanned_block` per stage, `transactions.next_index`, etc. |
| **`progress.json`** | Human-readable: phase, % of block range, blocks/sec, rough ETA |
| **Stdout** | Every N chunks, same stats printed |
| **Incremental JSONL** | Log phases append per chunk (fsync); no giant in-memory lists |
| **`tx_from_cache.jsonl` / `signer_node_cache.jsonl`** | Append-only caches so tx/signer phases can resume |
| **Streaming** | Metrics CSV + summary built by **streaming** JSONL (constant memory) |

If the process dies, **`--resume`** continues from the last completed chunk (discovery, each log stream, tx index, or signer batch). **`--resume`** with `phase=done` exits successfully (already finished).

## Notes

- Day 1 lower bound uses **L2 block 1** if there is no **`DayStartedEvent` with `dayId=1`**. `deploymentBlockNumber()` on the contract is **not** used for L2 bounds (it tracks parent-chain `block.number` on Arbitrum Nitro, unlike RPC block tags).
- Day 30 upper bound uses `blockStart(31)-1` when a day-31 `DayStartedEvent` exists; otherwise the latest L2 block at run time.
- Run under **`tmux`**, **`screen`**, or **`nohup`** on a stable host; keep RPC timeouts generous (script uses 180s HTTP timeout).
