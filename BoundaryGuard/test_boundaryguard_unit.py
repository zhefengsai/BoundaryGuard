#!/usr/bin/env python3
"""Unit tests for exact-prefix acceptance, cache reuse, and fail-closed paths."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from boundaryguard_proxy import (  # noqa: E402
    AuditUnavailable,
    BoundaryCache,
    BoundaryGuardProxy,
    ProxyConfig,
)
from hyperlane_identity import DISPATCH_TOPIC  # noqa: E402


MAILBOX = "0x" + "11" * 20
OTHER = "0x" + "22" * 20


def encode_dispatch(nonce: int, block: int) -> dict:
    # Minimal ABI-shaped Dispatch data: dynamic bytes header + 77-byte message.
    message = bytearray(77)
    message[1:5] = nonce.to_bytes(4, "big")
    message[5:9] = (10).to_bytes(4, "big")  # origin domain
    body = (32).to_bytes(32, "big") + (77).to_bytes(32, "big") + bytes(message)
    return {
        "address": MAILBOX,
        "topics": [DISPATCH_TOPIC],
        "data": "0x" + body.hex(),
        "blockNumber": hex(block),
        "transactionHash": "0x" + "ab" * 32,
        "blockHash": "0x" + "cd" * 32,
        "logIndex": "0x0",
    }


class FakeRPC:
    def __init__(self):
        self.nonces: dict[int, int] = {}
        self.logs_by_range: dict[tuple[int, int], list[dict]] = {}
        self.fail_boundary = False
        self.fail_repair = False
        self.calls: list[tuple[str, str]] = []

    def handle(self, role: str, payload: dict) -> dict:
        self.calls.append((role, payload.get("method", "")))
        if payload.get("method") == "eth_call":
            if self.fail_boundary and role == "boundary":
                return {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"message": "down"}}
            block = int(payload["params"][1], 16)
            return {"jsonrpc": "2.0", "id": payload.get("id"), "result": hex(self.nonces.get(block, 0))}
        if payload.get("method") == "eth_getLogs":
            query = payload["params"][0]
            if "blockHash" in query:
                logs = [
                    log
                    for rows in self.logs_by_range.values()
                    for log in rows
                    if log.get("blockHash") == query["blockHash"]
                ]
                return {"jsonrpc": "2.0", "id": payload.get("id"), "result": logs}
            start, end = int(query["fromBlock"], 16), int(query["toBlock"], 16)
            if role == "repair" and self.fail_repair:
                return {"jsonrpc": "2.0", "id": payload.get("id"), "result": []}
            return {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "result": self.logs_by_range.get((start, end), []),
            }
        if payload.get("method") == "eth_getBlockByHash":
            return {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "result": {"number": "0x65", "hash": payload["params"][0]},
            }
        return {"jsonrpc": "2.0", "id": payload.get("id"), "result": "0x1"}


def serve_fake(fake: FakeRPC, role: str) -> tuple[ThreadingHTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length))
            body = json.dumps(fake.handle(role, payload)).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


class BoundaryGuardUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeRPC()
        self.servers = []
        urls = {}
        for role in ("primary", "boundary", "repair"):
            server, url = serve_fake(self.fake, role)
            self.servers.append(server)
            urls[role] = url
        self.cfg = ProxyConfig(
            chain_id="test",
            mailbox=MAILBOX,
            primary_url=urls["primary"],
            boundary_url=urls["boundary"],
            repair_url=urls["repair"],
            timeout_s=2.0,
            retry_count=0,
            chunk_count=4,
            trace_path=None,
        )
        self.proxy = BoundaryGuardProxy(self.cfg)

    def tearDown(self) -> None:
        for server in self.servers:
            server.shutdown()

    def test_exact_prefix_accept(self) -> None:
        # Nonces before 100 = 10, at 109 = 12 => expect nonces 10,11 in [100,109]
        self.fake.nonces[99] = 10
        self.fake.nonces[109] = 12
        logs = [encode_dispatch(10, 101), encode_dispatch(11, 105)]
        self.fake.logs_by_range[(100, 109)] = logs
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getLogs",
            "params": [{
                "address": MAILBOX,
                "topics": [DISPATCH_TOPIC],
                "fromBlock": hex(100),
                "toBlock": hex(109),
            }],
        }
        out = self.proxy.handle(payload)
        self.assertEqual(len(out["result"]), 2)

    def test_cache_reuse(self) -> None:
        self.fake.nonces[99] = 10
        self.fake.nonces[109] = 10
        self.fake.logs_by_range[(100, 109)] = []
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getLogs",
            "params": [{
                "address": MAILBOX,
                "topics": [DISPATCH_TOPIC],
                "fromBlock": hex(100),
                "toBlock": hex(109),
            }],
        }
        self.proxy.handle(payload)
        before = sum(1 for role, method in self.fake.calls if role == "boundary" and method == "eth_call")
        self.proxy.handle({**payload, "id": 2})
        after = sum(1 for role, method in self.fake.calls if role == "boundary" and method == "eth_call")
        self.assertEqual(after, before)

    def test_fail_closed_on_boundary_outage(self) -> None:
        self.fake.fail_boundary = True
        self.fake.logs_by_range[(100, 109)] = []
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getLogs",
            "params": [{
                "address": MAILBOX,
                "topics": [DISPATCH_TOPIC],
                "fromBlock": hex(100),
                "toBlock": hex(109),
            }],
        }
        with self.assertRaises(AuditUnavailable):
            self.proxy.handle(payload)

    def test_selective_repair_on_omission(self) -> None:
        # Expect nonces 10..13 across four chunks of width 10: [100,109]..[130,139]
        self.fake.nonces[99] = 10
        for end, value in [(109, 11), (119, 12), (129, 12), (139, 14)]:
            self.fake.nonces[end] = value
        # Primary omits chunk containing nonce 11 (block 115)
        self.fake.logs_by_range[(100, 139)] = [
            encode_dispatch(10, 105),
            encode_dispatch(12, 135),
            encode_dispatch(13, 136),
        ]
        # Repair returns only the missing chunk range when selectively queried.
        self.fake.logs_by_range[(110, 119)] = [encode_dispatch(11, 115)]
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getLogs",
            "params": [{
                "address": MAILBOX,
                "topics": [DISPATCH_TOPIC],
                "fromBlock": hex(100),
                "toBlock": hex(139),
            }],
        }
        out = self.proxy.handle(payload)
        from hyperlane_identity import inspect_dispatch
        nonces = sorted(inspect_dispatch(row, compute_hash=False)["nonce"] for row in out["result"])
        self.assertEqual(nonces, [10, 11, 12, 13])

    def test_block_hash_dispatch_audits_as_single_block(self) -> None:
        block_hash = "0x" + "cd" * 32
        self.fake.nonces[100] = 10
        self.fake.nonces[101] = 11
        log = encode_dispatch(10, 101)
        log["blockHash"] = block_hash
        self.fake.logs_by_range[(101, 101)] = [log]
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getLogs",
            "params": [{
                "address": MAILBOX,
                "topics": [DISPATCH_TOPIC],
                "blockHash": block_hash,
            }],
        }
        out = self.proxy.handle(payload)
        self.assertEqual(len(out["result"]), 1)
        self.assertEqual(self.proxy.stats["accepted"], 1)


class CacheTests(unittest.TestCase):
    def test_cache_put_get(self) -> None:
        cache = BoundaryCache()
        self.assertIsNone(cache.get("1", MAILBOX, 10))
        cache.put("1", MAILBOX, 10, 7)
        self.assertEqual(cache.get("1", MAILBOX, 10), 7)


if __name__ == "__main__":
    unittest.main()
