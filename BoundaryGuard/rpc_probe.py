#!/usr/bin/env python3
"""Small, reproducible RPC capability and range-cost probe.

The probe deliberately stays below one request per endpoint at a time.  It is
not a rate-limit stress test; it measures method semantics, range-dependent
latency, and provider capability differences using public EVM endpoints.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path


CHAINS = {
    "ethereum": {
        "chain_id": "0x1",
        "core": "0x98f3c9e6E3fAce36bAAd05FE09d375Ef1464288B",
        "block": "0x17e43a7",
        "tx": "0xff004874499070502b2a21bed1b6de7b24200ec0a25d94367f4dc4a889c3b3b5",
        "urls": {
            "1rpc": "https://public.1rpc.io/eth",
            "publicnode": "https://ethereum-rpc.publicnode.com",
            "drpc": "https://eth.drpc.org",
        },
    },
    "polygon": {
        "chain_id": "0x89",
        "core": "0x7A4B5a56256163F07b2C80A7cA55aBE66c4ec4d7",
        "block": "0x5293159",
        "tx": "0xa80d1bc3302871f63d492dc0b745a75059ee4fab71cd98ef92a4aa81a0ac19a4",
        "urls": {
            "1rpc": "https://public.1rpc.io/matic",
            "publicnode": "https://polygon-bor-rpc.publicnode.com",
            "drpc": "https://polygon.drpc.org",
        },
    },
    "arbitrum": {
        "chain_id": "0xa4b1",
        "core": "0xa5f208e072434bC67592E4C49C1B991BA79BCA46",
        "block": "0x1b76c565",
        "tx": "0x29c4047433908cad6a11ec3d5a5cca038dcb5a7f4fb13da80c6904ce5b9a2d00",
        "urls": {
            "1rpc": "https://public.1rpc.io/arb",
            "publicnode": "https://arbitrum-one-rpc.publicnode.com",
            "drpc": "https://arbitrum.drpc.org",
        },
    },
    "optimism": {
        "chain_id": "0xa",
        "core": "0xEe91C335eab126dF5fDB3797EA9d6aD93aeC9722",
        "block": None,
        "tx": "0xdf9bc6f4511be44adc8a69a5d4d4afdca8bdfb65fa5455793ff8fad66394b971",
        "urls": {
            "1rpc": "https://public.1rpc.io/op",
            "publicnode": "https://optimism-rpc.publicnode.com",
            "drpc": "https://optimism.drpc.org",
        },
    },
}

TOPIC0 = "0x6eb224fb001ed210e379b335e35efe88672a8ce935d981a6896b27ffdf52a3b2"


def rpc(url: str, method: str, params: list, timeout: float) -> dict:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "BoundaryGuard-rpc-probe/0.1"},
        method="POST",
    )
    started = time.perf_counter()
    status = None
    raw = b""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            status = res.status
            raw = res.read()
        elapsed = (time.perf_counter() - started) * 1000
        body = json.loads(raw)
        if "error" in body:
            outcome = "rpc_error"
        elif body.get("result") is None:
            outcome = "null"
        else:
            outcome = "ok"
        return {
            "outcome": outcome,
            "http_status": status,
            "latency_ms": round(elapsed, 3),
            "response_bytes": len(raw),
            "result": body.get("result"),
            "error": body.get("error"),
        }
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        elapsed = (time.perf_counter() - started) * 1000
        try:
            body = json.loads(raw)
        except Exception:
            body = None
        return {
            "outcome": "http_error",
            "http_status": exc.code,
            "latency_ms": round(elapsed, 3),
            "response_bytes": len(raw),
            "result": None,
            "error": body if body is not None else raw.decode(errors="replace")[:500],
        }
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        return {
            "outcome": "transport_error",
            "http_status": status,
            "latency_ms": round(elapsed, 3),
            "response_bytes": len(raw),
            "result": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def compact_record(base: dict, response: dict) -> dict:
    result = response.pop("result", None)
    if isinstance(result, list):
        response["result_count"] = len(result)
    elif isinstance(result, dict):
        response["result_kind"] = "object"
    elif isinstance(result, str):
        response["result_kind"] = "hex_or_string"
    elif result is not None:
        response["result_kind"] = type(result).__name__
    return {**base, **response}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("work/data/rpc_probe.jsonl"))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--ranges", default="1,50,100,1000,10000")
    parser.add_argument("--delay", type=float, default=0.08)
    parser.add_argument("--range-only", action="store_true")
    args = parser.parse_args()
    ranges = [int(x) for x in args.ranges.split(",")]
    args.output.parent.mkdir(parents=True, exist_ok=True)

    records: list[dict] = []
    blocks: dict[tuple[str, str], str | None] = {}
    latest_blocks: dict[tuple[str, str], str | None] = {}
    hashes: dict[tuple[str, str], str | None] = {}

    def call(chain: str, provider: str, method: str, params: list, **meta: object) -> dict:
        response = rpc(CHAINS[chain]["urls"][provider], method, params, args.timeout)
        rec = compact_record(
            {
                "ts": time.time(),
                "chain": chain,
                "provider": provider,
                "method": method,
                **meta,
            },
            response,
        )
        records.append(rec)
        print(
            f"{chain:9s} {provider:10s} {method:28s} "
            f"{rec['outcome']:15s} {rec['latency_ms']:9.1f} ms",
            flush=True,
        )
        time.sleep(args.delay)
        return rec

    # Endpoint identity and a usable block for each endpoint.
    for chain, cfg in CHAINS.items():
        for provider in cfg["urls"]:
            cid = call(chain, provider, "eth_chainId", [], probe="identity")
            latest_raw = rpc(cfg["urls"][provider], "eth_blockNumber", [], args.timeout)
            latest = latest_raw.get("result")
            records.append(compact_record({
                "ts": time.time(), "chain": chain, "provider": provider,
                "method": "eth_blockNumber", "probe": "identity",
            }, latest_raw))
            latest_blocks[(chain, provider)] = latest
            blocks[(chain, provider)] = cfg["block"] or latest
            if cid["outcome"] == "ok" and cid.get("result_kind") != "hex_or_string":
                blocks[(chain, provider)] = None

    # Capability and semantic probes on a known transaction/block.
    if not args.range_only:
        for chain, cfg in CHAINS.items():
            for provider in cfg["urls"]:
                block = blocks[(chain, provider)]
                call(chain, provider, "eth_getTransactionReceipt", [cfg["tx"]], probe="semantics")
                if not block:
                    continue
                block_response = rpc(cfg["urls"][provider], "eth_getBlockByNumber", [block, False], args.timeout)
                block_result = block_response.get("result")
                hashes[(chain, provider)] = block_result.get("hash") if isinstance(block_result, dict) else None
                records.append(compact_record({
                    "ts": time.time(), "chain": chain, "provider": provider,
                    "method": "eth_getBlockByNumber", "probe": "semantics",
                }, block_response))
                call(chain, provider, "eth_getBlockReceipts", [block], probe="capability")
                block_hash = hashes[(chain, provider)]
                if block_hash:
                    call(
                        chain,
                        provider,
                        "eth_getLogs",
                        [{"blockHash": block_hash, "address": cfg["core"], "topics": [TOPIC0]}],
                        probe="block_hash_semantics",
                    )

    # Range-dependent eth_getLogs latency/errors.  The inclusive range has the
    # exact requested number of blocks and ends at the known/most recent block.
    for chain, cfg in CHAINS.items():
        for provider in cfg["urls"]:
            # Use the endpoint's current tip for service-cost measurements.  A
            # historical fixed block is retained only for semantic/capability
            # probes because archive access is a distinct provider feature.
            end_hex = latest_blocks[(chain, provider)]
            if not end_hex:
                continue
            end = int(end_hex, 16)
            for width in ranges:
                start = max(0, end - width + 1)
                filt = {
                    "fromBlock": hex(start),
                    "toBlock": hex(end),
                    "address": cfg["core"],
                    "topics": [TOPIC0],
                }
                for repeat in range(args.repeats):
                    call(
                        chain,
                        provider,
                        "eth_getLogs",
                        [filt],
                        probe="range_cost",
                        range_blocks=width,
                        repeat=repeat,
                    )

    with args.output.open("w", encoding="utf-8") as handle:
        for rec in records:
            handle.write(json.dumps(rec, sort_keys=True) + "\n")

    print(f"\nWrote {len(records)} records to {args.output}")
    range_ok: dict[tuple[str, str, int], list[float]] = {}
    for rec in records:
        if rec.get("probe") == "range_cost" and rec["outcome"] == "ok":
            key = (rec["chain"], rec["provider"], rec["range_blocks"])
            range_ok.setdefault(key, []).append(rec["latency_ms"])
    for key, values in sorted(range_ok.items()):
        print(f"{key}: median={statistics.median(values):.1f} ms, n={len(values)}")


if __name__ == "__main__":
    main()
