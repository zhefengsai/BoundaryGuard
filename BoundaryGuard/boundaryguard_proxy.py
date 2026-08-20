#!/usr/bin/env python3
"""Transparent JSON-RPC proxy for BoundaryGuard.

Audits Hyperlane ``eth_getLogs`` ranges addressed to the configured Mailbox.
On an exact-prefix mismatch it selectively localizes inconsistent chunks,
repairs only those chunks from an independent repair provider, and returns the
merged response after the same prefix check.  Failures are fail-closed.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from hyperlane_identity import DISPATCH_TOPIC, inspect_dispatch, normalize


NONCE_SELECTOR = "0xaffed0e0"


class AuditUnavailable(RuntimeError):
    """Raised when completeness cannot be established; never fail open."""


def rpc(url: str, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", "user-agent": "BoundaryGuard/0.2"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read()
            parsed = json.loads(raw)
            parsed["_bg_bytes"] = len(raw)
            return parsed
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise AuditUnavailable(f"RPC unavailable: {type(exc).__name__}: {exc}") from exc


@dataclass
class BoundaryCache:
    values: dict[tuple[str, str, int], int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, chain_id: str, mailbox: str, block: int) -> int | None:
        with self.lock:
            return self.values.get((chain_id, mailbox, block))

    def put(self, chain_id: str, mailbox: str, block: int, value: int) -> None:
        with self.lock:
            self.values[(chain_id, mailbox, block)] = value

    def invalidate(self, chain_id: str, mailbox: str) -> None:
        with self.lock:
            keys = [key for key in self.values if key[0] == chain_id and key[1] == mailbox]
            for key in keys:
                del self.values[key]

    def clear(self) -> None:
        with self.lock:
            self.values.clear()


@dataclass
class ProxyConfig:
    chain_id: str
    mailbox: str
    primary_url: str
    boundary_url: str
    repair_url: str
    timeout_s: float = 20.0
    retry_count: int = 2
    retry_base_s: float = 0.25
    chunk_count: int = 8
    confirmation_depth: int = 64
    finalized_tip: int | None = None
    byzantine_inclusion: bool = False
    trace_path: str | None = None
    # Optional archive-capable boundary fallbacks tried in order after boundary_url.
    boundary_urls: list[str] | None = None
    # Controlled S2/S3 fault injection applied to primary eth_getLogs before audit.
    # Modes: none|successful_empty|omit_nth|truncate_last
    # Optional repair_fault_injection.mode=successful_empty forces correlated
    # primary+repair emptiness => fail-closed (unavailable), never incomplete release.
    fault_injection: dict[str, Any] | None = None
    repair_fault_injection: dict[str, Any] | None = None

    def boundary_candidates(self) -> list[str]:
        ordered: list[str] = []
        for url in [self.boundary_url, *(self.boundary_urls or [])]:
            if url and url not in ordered:
                ordered.append(url)
        return ordered

    @classmethod
    def load(cls, path: Path) -> "ProxyConfig":
        raw = json.loads(path.read_text())
        cfg = cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})
        cfg.mailbox = normalize(cfg.mailbox) or ""
        urls = {cfg.primary_url, cfg.boundary_url, cfg.repair_url}
        if len(urls) != 3:
            raise ValueError("primary, boundary, and repair URLs must be distinct")
        return cfg


@dataclass
class TraceSink:
    path: Path | None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def write(self, record: dict[str, Any]) -> None:
        if self.path is None:
            return
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")


class BoundaryGuardProxy:
    def __init__(self, config: ProxyConfig):
        self.config = config
        self.cache = BoundaryCache()
        self.trace = TraceSink(Path(config.trace_path) if config.trace_path else None)
        self.stats = {
            "requests": 0,
            "audited": 0,
            "accepted": 0,
            "repaired": 0,
            "unavailable": 0,
            "boundary_calls": 0,
            "repair_calls": 0,
            "response_bytes": 0,
        }

    def _request_with_retry(self, url: str, payload: dict[str, Any], role: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.config.retry_count + 1):
            try:
                response = rpc(url, payload, self.config.timeout_s)
                self.stats["response_bytes"] += int(response.pop("_bg_bytes", 0))
                if role == "boundary":
                    self.stats["boundary_calls"] += 1
                if role == "repair":
                    self.stats["repair_calls"] += 1
                if "error" in response:
                    raise AuditUnavailable(f"JSON-RPC error: {response['error']}")
                return response
            except AuditUnavailable as exc:
                last_error = exc
                if attempt < self.config.retry_count:
                    time.sleep(self.config.retry_base_s * (2**attempt))
        raise AuditUnavailable(str(last_error))

    def _nonce(self, block: int, request_id: Any) -> int:
        cached = self.cache.get(self.config.chain_id, self.config.mailbox, block)
        if cached is not None:
            return cached
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "eth_call",
            "params": [{"to": self.config.mailbox, "data": NONCE_SELECTOR}, hex(block)],
        }
        last_error: Exception | None = None
        for url in self.config.boundary_candidates():
            try:
                response = self._request_with_retry(url, payload, "boundary")
                value = response.get("result")
                if not isinstance(value, str):
                    raise AuditUnavailable("boundary provider returned no nonce")
                nonce = int(value, 16)
                self.cache.put(self.config.chain_id, self.config.mailbox, block, nonce)
                return nonce
            except AuditUnavailable as exc:
                last_error = exc
                continue
        raise AuditUnavailable(
            f"all boundary providers unavailable: {last_error}"
        )

    def _verified_events(self, logs: Any, start: int, end: int) -> list[dict[str, Any]]:
        if not isinstance(logs, list):
            raise AuditUnavailable("log result is not a list")
        events: list[dict[str, Any]] = []
        seen: set[int] = set()
        for log in logs:
            if normalize(log.get("address")) != self.config.mailbox:
                raise AuditUnavailable("foreign contract in log result")
            identity = inspect_dispatch(log, compute_hash=False)
            block = identity["block_number"]
            if block is None or not start <= block <= end:
                raise AuditUnavailable("event block outside requested interval")
            nonce = identity["nonce"]
            if nonce in seen:
                raise AuditUnavailable("duplicate Hyperlane nonce")
            seen.add(nonce)
            events.append({"nonce": nonce, "block": block, "log": log, "identity": identity})
        events.sort(key=lambda row: row["nonce"])
        return events

    def _exact_prefix(self, logs: Any, start: int, end: int, request_id: Any) -> tuple[bool, list[dict[str, Any]], int, int]:
        before = self._nonce(start - 1, request_id)
        after = self._nonce(end, request_id)
        events = self._verified_events(logs, start, end)
        nonces = [row["nonce"] for row in events]
        return nonces == list(range(before, after)), events, before, after

    def _chunk_bounds(self, start: int, end: int) -> list[tuple[int, int]]:
        width = max(1, (end - start + 1 + self.config.chunk_count - 1) // self.config.chunk_count)
        chunks = []
        cursor = start
        while cursor <= end:
            chunk_end = min(end, cursor + width - 1)
            chunks.append((cursor, chunk_end))
            cursor = chunk_end + 1
        return chunks

    def _locate(
        self,
        chunks: list[tuple[int, int]],
        observed: list[int],
        request_id: Any,
        left: int = 0,
        right: int | None = None,
        start_nonce: int | None = None,
        end_nonce: int | None = None,
    ) -> list[int]:
        right = len(chunks) if right is None else right
        start_nonce = self._nonce(chunks[left][0] - 1, request_id) if start_nonce is None else start_nonce
        end_nonce = self._nonce(chunks[right - 1][1], request_id) if end_nonce is None else end_nonce
        if end_nonce - start_nonce == sum(observed[left:right]):
            return []
        if right - left == 1:
            return [left]
        mid = (left + right) // 2
        mid_nonce = self._nonce(chunks[mid - 1][1], request_id)
        return (
            self._locate(chunks, observed, request_id, left, mid, start_nonce, mid_nonce)
            + self._locate(chunks, observed, request_id, mid, right, mid_nonce, end_nonce)
        )

    def _check_finality(self, end: int) -> None:
        tip = self.config.finalized_tip
        if tip is None:
            return
        if end > tip - self.config.confirmation_depth:
            raise AuditUnavailable("range newer than configured finalized tip")

    def _block_number_for_hash(
        self, block_hash: str, primary: dict[str, Any], request_id: Any
    ) -> int:
        for log in primary.get("result") or []:
            block_number = log.get("blockNumber")
            if isinstance(block_number, str):
                return int(block_number, 16)
        response = self._request_with_retry(
            self.config.primary_url,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "eth_getBlockByHash",
                "params": [block_hash, False],
            },
            "primary",
        )
        block = response.get("result")
        if not isinstance(block, dict) or not isinstance(block.get("number"), str):
            raise AuditUnavailable("cannot resolve blockHash for audited Dispatch query")
        return int(block["number"], 16)

    def _apply_fault_injection(
        self, primary: dict[str, Any], case_id: str, start: int, end: int
    ) -> dict[str, Any]:
        """Mutate primary eth_getLogs result before audit (controlled_fault_injection)."""
        cfg = self.config.fault_injection or {}
        mode = (cfg.get("mode") or "none").lower()
        if mode in ("", "none"):
            return primary
        logs = list(primary.get("result") or [])
        original = len(logs)
        if mode == "successful_empty":
            mutated: list[Any] = []
        elif mode == "omit_nth":
            n = int(cfg.get("n", 0))
            mutated = [row for i, row in enumerate(logs) if i != n]
        elif mode == "truncate_last":
            k = max(0, int(cfg.get("k", 1)))
            mutated = logs[:-k] if k else list(logs)
        else:
            raise AuditUnavailable(f"unknown fault_injection.mode: {mode}")
        self.trace.write({
            "case_id": case_id,
            "method": "eth_getLogs",
            "outcome": "fault_injected",
            "fault_mode": mode,
            "fault_params": {k: v for k, v in cfg.items() if k != "mode"},
            "start": start,
            "end": end,
            "primary_original_count": original,
            "primary_mutated_count": len(mutated),
            "provenance": "controlled_fault_injection",
        })
        out = dict(primary)
        out["result"] = mutated
        return out

    def _apply_repair_fault(
        self, repaired: dict[str, Any], case_id: str, start: int, end: int
    ) -> dict[str, Any]:
        cfg = self.config.repair_fault_injection or {}
        mode = (cfg.get("mode") or "none").lower()
        if mode in ("", "none"):
            return repaired
        if mode == "successful_empty":
            mutated: list[Any] = []
        else:
            raise AuditUnavailable(f"unknown repair_fault_injection.mode: {mode}")
        self.trace.write({
            "case_id": case_id,
            "method": "eth_getLogs",
            "outcome": "repair_fault_injected",
            "fault_mode": mode,
            "start": start,
            "end": end,
            "repair_original_count": len(repaired.get("result") or []),
            "repair_mutated_count": len(mutated),
            "provenance": "controlled_fault_injection",
        })
        out = dict(repaired)
        out["result"] = mutated
        return out

    def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        case_id = str(uuid.uuid4())
        started = time.time()
        self.stats["requests"] += 1
        primary = self._request_with_retry(self.config.primary_url, payload, "primary")
        if payload.get("method") != "eth_getLogs" or not payload.get("params"):
            return primary
        query = payload["params"][0]
        if normalize(query.get("address")) != self.config.mailbox:
            return primary
        topics = query.get("topics") or []
        # Only audit typed Dispatch queries. Unfiltered Mailbox getLogs (used by
        # Hyperlane tx-id indexing) must pass through; auditing them 503s the agent.
        if not topics or normalize(topics[0]) != DISPATCH_TOPIC:
            return primary
        # Hyperlane re-fetches Dispatch by blockHash after merkle tx-id indexing.
        # Resolve to a numeric single-block range so we can still exact-prefix audit.
        if "blockHash" in query:
            start = end = self._block_number_for_hash(
                query["blockHash"], primary, payload.get("id")
            )
        else:
            try:
                start, end = int(query["fromBlock"], 16), int(query["toBlock"], 16)
            except (KeyError, TypeError, ValueError) as exc:
                raise AuditUnavailable("audited range must use numeric block tags") from exc
        self._check_finality(end)
        self.stats["audited"] += 1
        primary = self._apply_fault_injection(primary, case_id, start, end)
        ok, events, before, after = self._exact_prefix(primary.get("result"), start, end, payload.get("id"))
        if ok:
            self.stats["accepted"] += 1
            self.trace.write({
                "case_id": case_id,
                "method": "eth_getLogs",
                "outcome": "accept",
                "start": start,
                "end": end,
                "expected": after - before,
                "observed": len(events),
                "latency_ms": (time.time() - started) * 1000,
                "boundary_calls": 2,
                "repair_calls": 0,
            })
            return primary

        chunks = self._chunk_bounds(start, end)
        observed_counts = []
        for chunk_start, chunk_end in chunks:
            count = sum(1 for row in events if chunk_start <= row["block"] <= chunk_end)
            observed_counts.append(count)
        bad = self._locate(chunks, observed_counts, payload.get("id"))
        repaired_logs = list(primary.get("result") or [])
        # Drop events from bad chunks before merging repaired logs.
        keep = []
        bad_ranges = {chunks[i] for i in bad}
        for log in repaired_logs:
            block = int(log["blockNumber"], 16)
            if any(lo <= block <= hi for lo, hi in bad_ranges):
                continue
            keep.append(log)
        repaired_logs = keep
        range_query = {key: value for key, value in query.items() if key != "blockHash"}
        for index in bad:
            chunk_start, chunk_end = chunks[index]
            repair_payload = {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "method": "eth_getLogs",
                "params": [{
                    **range_query,
                    "fromBlock": hex(chunk_start),
                    "toBlock": hex(chunk_end),
                }],
            }
            repaired = self._request_with_retry(self.config.repair_url, repair_payload, "repair")
            repaired = self._apply_repair_fault(repaired, case_id, chunk_start, chunk_end)
            repaired_logs.extend(repaired.get("result") or [])
        ok, events, before, after = self._exact_prefix(repaired_logs, start, end, payload.get("id"))
        if not ok:
            self.stats["unavailable"] += 1
            self.trace.write({
                "case_id": case_id,
                "method": "eth_getLogs",
                "outcome": "unavailable",
                "start": start,
                "end": end,
                "bad_chunks": bad,
                "latency_ms": (time.time() - started) * 1000,
                "repair_calls": len(bad),
                "reason": "repair response does not form the expected prefix",
                "provenance": "controlled_fault_injection"
                if (self.config.fault_injection or self.config.repair_fault_injection)
                else "live_natural_observation",
            })
            raise AuditUnavailable("repair response does not form the expected prefix")
        self.stats["repaired"] += 1
        self.trace.write({
            "case_id": case_id,
            "method": "eth_getLogs",
            "outcome": "repaired",
            "start": start,
            "end": end,
            "bad_chunks": bad,
            "expected": after - before,
            "observed": len(events),
            "latency_ms": (time.time() - started) * 1000,
            "repair_calls": len(bad),
        })
        return {"jsonrpc": "2.0", "id": payload.get("id"), "result": [row["log"] for row in events]}


def make_handler(proxy: BoundaryGuardProxy):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            payload: dict[str, Any] | None = None
            try:
                length = int(self.headers.get("content-length", "0"))
                payload = json.loads(self.rfile.read(length))
                if isinstance(payload, list):
                    raise AuditUnavailable("batch JSON-RPC is not supported by the minimal proxy")
                response = proxy.handle(payload)
                status = 200
            except Exception as exc:
                proxy.stats["unavailable"] += 1
                request_id = payload.get("id") if isinstance(payload, dict) else None
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32071, "message": f"BoundaryGuard unavailable: {exc}"},
                }
                status = 503
            body = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8545)
    args = parser.parse_args()
    proxy = BoundaryGuardProxy(ProxyConfig.load(args.config))
    server = ThreadingHTTPServer((args.listen, args.port), make_handler(proxy))
    print(f"BoundaryGuard proxy listening on http://{args.listen}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
