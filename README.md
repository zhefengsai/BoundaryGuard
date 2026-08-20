# BoundaryGuard: Protocol-State Auditing for Reliable Cross-Chain Message Retrieval

This directory is the public code and data release for the paper of the same title. It contains the proxy, the frozen traces cited in the evaluation, and the scripts that produced those traces. RPC credentials and private keys are not included.

Cross-chain relayers fetch source-chain events from remotely operated RPC services. An HTTP or JSON-RPC success code means the transport succeeded. It does not mean the body contains every protocol event implied by committed contract state. The paper calls that stronger requirement semantic completeness. In targeted live probes of Hyperlane `Mailbox.Dispatch` retrieval, 58 of 662 queries returned a successful but incomplete body. Independently operated provider organizations also returned matching successful-empty replies for the same nonce.

BoundaryGuard is a JSON-RPC proxy that preserves the `eth_getLogs` schema. It audits each reply against an independently obtained monotone boundary, `Mailbox.nonce()`. When the exact prefix does not match, it isolates the inconsistent chunks, repairs only those chunks from a second provider, and returns unavailable if completeness cannot be established.

## Directory layout

| Path | Contents |
|------|----------|
| [`BoundaryGuard/`](BoundaryGuard/) | Proxy (`boundaryguard_proxy.py`), identity checks (`hyperlane_identity.py`), `rpc_probe.py`, unit tests, and an example config. |
| [`data/`](data/) | Frozen traces and summaries, including `experiment_manifest.json` (SHA-256 inventory). |
| [`exp/`](exp/) | Experiment drivers: natural-fault collection, paired baselines, throughput, testnet integration, freeze, and verify. |
| [`exp/bin/`](exp/bin/) | Optional shell helpers for testnet runs. They do not contain funded keys. |

## Experimental conditions

The paper asks five questions.

1. RQ1 (Reality). Do successful RPC responses exhibit semantic incompleteness?
2. RQ2 (Correlated retrieval). Can replica agreement fail to expose the same omission?
3. RQ3 (Correctness and recovery). Does BoundaryGuard accept complete evidence without false alarms, and does it detect, localize, repair, or safely withhold incomplete evidence?
4. RQ4 (Cost and performance). What cost, latency, and throughput trade-off does protocol-state auditing impose relative to unchecked, fallback, and always-dual retrieval?
5. RQ5 (Integration and applicability). Does the design work with an unmodified relayer, and under what conditions can the same abstraction be used beyond Hyperlane?

The proxy sits in front of unmodified `eth_getLogs` clients, including Hyperlane `agents-v2.2.0`. Measurements use Ethereum, Optimism, Polygon, and Arbitrum. Closed-loop delivery is Optimism Sepolia to Ethereum Sepolia. The test ISM is a 1-of-1 MessageIdMultisigISM under our validator. That configuration checks integration. It is not a claim about decentralized production security.

Each printed claim has one provenance label (table below). A saved failure that is later replayed is not counted as a new live observation. Gate 1 is the four-chain live audit. The Gate 2 figures in the paper come from `data/natural_fault_gate2_submit_freeze.json`: 4,416 responses on UTC 2026-08-03, 2026-08-04, 2026-08-05, 2026-08-06, and 2026-08-18. The collector file `data/natural_fault_gate2_summary.json` also includes 2026-08-19 (4,896 records) and is not the printed corpus. The 58/662 rate is an occurrence count on a targeted schedule, not an estimate of how often silent omissions occur on the public Internet.

The harness compares four policies. Unchecked retrieval uses a single provider. Explicit-error fallback retries only on reported errors. Always-dual retrieval takes the larger of two log sets and is used as a cost baseline. A 2-of-3 quorum is replayed from saved Gate 2 replies; that replay is not an official Hyperlane quorum-mode agent run. The official-agent experiment measures integration, not throughput. Cost is reported in Alchemy reference compute units (60 for `eth_getLogs`, 26 for `eth_call`). Those values are a published reference, not a universal tariff. The evaluation also reports false-alarm rate, exact-chunk localization, availability, proxy latency, and throughput.

RQ1. Targeted probes cover 25 chain/provider pairs and 64 real messages, using both the block-hash and single-block `eth_getLogs` forms. 58 of 662 queries were successful and incomplete. On the printed Gate 2 freeze the same two organizations (1rpc, flashbots) produced 145 successful-empty replies and 611 nonempty replies that missed the scheduled Dispatch (32 distinct nonces). A bounded search did not find a third organization with successful-empty behavior.

RQ2. Independently operated providers can return matching successful-empty bodies for the same nonce (`data/natural_correlated_dual.json`). Agreement among replicas is therefore not a completeness proof. A 2-of-3 replay over 448 Gate 2 groups is stored in `data/quorum_trace_baseline.json`. In controlled dual-silent cases, always-dual remains silent while BoundaryGuard detects the gap and repairs it.

RQ3. Of 1,281 live Gate 1 batches, 1,141 were auditable and none raised a false alarm. 140 batches withheld under a single-boundary ablation later recovered 140/140 on the multi-URL archive path. All 28 saved silent omissions in `data/natural_e2e_expanded.jsonl` repair under live boundary and repair RPCs. Ten single-block empty windows recover 10/10. Controlled identity mutations cover the integrity faults in the paper's scope. A four-chain `Mailbox.nonce()` cross-check at 100 heights per chain agrees in 400 of 400 readable pairs.

RQ4. A seeded fault-density matrix of 660 controlled scenarios localizes every case. Cold healthy-path baselines use five disjoint Optimism slices of 198 messages each, with a post-hoc oracle on 691 unique blocks (at least two provider organizations). Relative to always-dual, the archive proxy saves 20.1% reference CU on that cold path; P50/P95 rise from 0.17/0.25 s to 0.55/0.87 s. Concurrent runs use n = 64 queries at concurrency 1 and 8. At concurrency 8 the archive proxy uses 7,116 CU against 7,680 for always-dual (7.3% lower), at 9.26 qps versus 19.4 qps and P95 1.92 s versus 0.47 s. Sliding-window cache amortization and a controlled demotion state machine (omit, quarantine, probation, healthy) are reported separately. On ten preserved successful-empty contexts, unchecked retrieval misses all ten; always-dual and BoundaryGuard recover all ten.

RQ5. An unmodified official Hyperlane agent delivered 50 valid trials out of 59 attempts. Nine attempts were excluded because source Dispatch failed before the proxy path ran. Ten further trials in which both primary and repair returned empty failed closed; those ten are not among the 59. On 12 live Ethereum Wormhole Core windows, `nextSequence(emitter)` matches `LogMessagePublished` 12/12, and a controlled single-event drop is detected 12/12. That experiment tests the monotone-state mapping. It is not a Wormhole relayer integration.

Re-running live RPC jobs produces new timestamps and, in general, different counts. The printed numbers are those in the frozen files named above.

## Provenance labels

| Label | Meaning |
|------|---------|
| `live_natural_observation` | Unmodified public RPC response collected live |
| `live_testnet_execution` | Closed-loop testnet delivery through the proxy |
| `controlled_testnet_fault_injection` | Deliberate fault during testnet end-to-end runs |
| `saved_natural_failure_live_replay` | Saved natural failure at its original block, with live boundary and repair |
| `controlled_fault_injection` | Deliberately mutated canonical trace |
| `controlled_integrity_mutation` | Deliberately mutated message identity set |

## Requirements

Python 3.10 or later. The published proxy and scripts use the standard library only.

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
```

Live re-runs need reachable HTTPS RPC endpoints. Offline checks read `data/` only. Do not commit `.env`, `*.key`, or `boundaryguard_proxy.local.json`.

## Running the proxy

```bash
cp BoundaryGuard/boundaryguard_proxy.example.json BoundaryGuard/boundaryguard_proxy.local.json
# Edit three distinct RPC URLs and the Mailbox address. Do not commit this file.
python3 BoundaryGuard/boundaryguard_proxy.py \
  --config BoundaryGuard/boundaryguard_proxy.local.json \
  --listen 127.0.0.1 --port 8545
```

Point an unmodified `eth_getLogs` client, for example a Hyperlane agent source RPC, at `http://127.0.0.1:8545`.

## Checking the frozen files

```bash
python3 exp/verify_manifest.py
# expect: OK

python3 -c "import json; print(json.load(open('data/natural_fault_gate2_submit_freeze.json')))"
# expect: records=4416, five calendar days, exclude 2026-08-19
```

## Reproducing the experiments

Run the following from this directory. Offline commands read frozen files and do not contact RPC. Live commands use public RPCs; their outputs will not match the paper's timestamps. Several scripts rewrite JSON under `data/` and will therefore change the manifest hashes. To check a printed number, read the frozen file. Run `exp/freeze_experiment.py` only if you intend to refresh the inventory.

### Unit tests

```bash
python3 BoundaryGuard/test_boundaryguard_unit.py
```

### RQ1. Natural incompleteness

```bash
python3 -c "import json; d=json.load(open('data/natural_fault_summary.json')); print(d['silent_semantic_failures'], d['unique_query_contexts'], d['messages'])"
# expect: 58 662 64

python3 exp/gate2_calendar_inventory.py
# optional calendar inventory; the printed freeze remains data/natural_fault_gate2_submit_freeze.json
```

Live collection appends or overwrites traces. New rows are not the printed freeze.

```bash
python3 exp/natural_fault_expansion.py
python3 exp/natural_fault_multiday.py
python3 exp/third_org_hunt_bounded.py
```

### RQ2. Correlated retrieval

The printed 2-of-3 comparison is an offline replay:

```bash
python3 exp/quorum_trace_baseline.py
python3 exp/dual_correlated_cost_gates.py
```

Natural correlated-empty pairs: `data/natural_correlated_dual.json`. Controlled always-dual silent miss versus BoundaryGuard detection: `data/correlated_fault_gate.json` and `data/dual_silent_empty_gate.json`.

### RQ3. Correctness and recovery

```bash
python3 exp/strong_accept_suite.py
python3 exp/identity_gate.py
```

Live public RPC:

```bash
python3 exp/boundary_fallback_reprobe.py
python3 exp/natural_e2e_expand.py
python3 exp/scale_four_chain_1000.py
python3 exp/boundary_nonce_crosscheck_4chain.py --per-chain 100
```

Repair-path records: `data/natural_e2e_expanded.jsonl` (28 saved silent omissions). Gate 1 four-chain summary: `data/boundaryguard_scale_four_chain_summary.json`.

### RQ4. Cost and performance

```bash
python3 exp/strong_accept_suite.py
python3 exp/throughput_root_cause.py
python3 exp/demotion_fsm_controlled.py
```

Cold table in the paper: 198 messages, five disjoint slices.

```bash
python3 exp/s4_paired_baselines.py --limit 198 --reps 5
python3 exp/freeze_s4_ground_truth.py --resume
python3 exp/concurrent_throughput.py --n 64 --concurrencies 1,8
python3 exp/contiguous_scan_amortization.py
```

`s4_paired_baselines.py --skip-live` runs only the controlled matrix and exits non-zero by design. Frozen concurrent rows: `data/strong_e2e/concurrent_throughput.json`.

### RQ5. Relayer integration and portability

Testnet runs need your own RPC and signer. Official-agent paths need Docker and Hyperlane `agents-v2.2.0`. The published test ISM is 1-of-1.

```bash
python3 exp/s1_go_nogo.py
python3 exp/s2_strong_e2e.py
python3 exp/s3_strong_e2e.py
python3 exp/bin/gate3_testnet_run.sh
```

Excluded official-agent attempts: `data/strong_e2e/excluded_attempts_classification.json`.

Wormhole portability (live Ethereum public RPC). Frozen result: `data/wormhole_live_boundary.json`.

```bash
python3 exp/wormhole_min_check.py --windows 12
```

The script checks that `nextSequence(emitter)` deltas match `LogMessagePublished` counts and that a controlled drop is detected. It does not run a Wormhole relayer.

## Refreshing the SHA-256 manifest

After adding files under `data/`:

```bash
python3 exp/freeze_experiment.py
python3 exp/verify_manifest.py
```

## Scope

This tree does not include private keys, `.env` files, funded testnet signers, operator RPC credentials, or the manuscript source. Raw Wormhole transaction dumps (tens of megabytes) are omitted; the printed portability result is `data/wormhole_live_boundary.json`. The release does not estimate the prevalence of silent omissions on the public Internet. The files in this repository are licensed under CC BY 4.0 (see `LICENSE`).
