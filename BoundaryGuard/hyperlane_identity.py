#!/usr/bin/env python3
"""Hyperlane Dispatch identity decoding and verification helpers."""

from __future__ import annotations

import subprocess


DISPATCH_TOPIC = "0x769f711d20c679153d382254f59892613b58a97cc876b249134ac25c80f9c814"


def normalize(value: str | None) -> str | None:
    if value is None:
        return None
    return "0x" + value.removeprefix("\\x").removeprefix("0x").lower()


def decode_dynamic_bytes(data: str) -> bytes:
    raw = bytes.fromhex(data.removeprefix("0x"))
    if len(raw) < 64:
        raise ValueError("event data shorter than ABI dynamic-bytes header")
    offset = int.from_bytes(raw[:32], "big")
    if offset + 32 > len(raw):
        raise ValueError("dynamic offset outside event data")
    length = int.from_bytes(raw[offset:offset + 32], "big")
    start = offset + 32
    if start + length > len(raw):
        raise ValueError("dynamic bytes truncated")
    return raw[start:start + length]


def decode_message(message: bytes) -> dict:
    if len(message) < 77:
        raise ValueError("Hyperlane message shorter than 77-byte header")
    return {
        "version": message[0],
        "nonce": int.from_bytes(message[1:5], "big"),
        "origin": int.from_bytes(message[5:9], "big"),
        "sender": "0x" + message[9:41].hex(),
        "destination": int.from_bytes(message[41:45], "big"),
        "recipient": "0x" + message[45:77].hex(),
        "body_hex": "0x" + message[77:].hex(),
        "raw_hex": "0x" + message.hex(),
    }


def message_id(raw_hex: str) -> str:
    completed = subprocess.run(
        ["cast", "keccak", raw_hex], check=True, capture_output=True, text=True,
    )
    return normalize(completed.stdout.strip()) or ""


def inspect_dispatch(log: dict, compute_hash: bool = True) -> dict:
    topics = [normalize(item) for item in log.get("topics", [])]
    if not topics or topics[0] != DISPATCH_TOPIC:
        raise ValueError("not a Hyperlane Dispatch event")
    decoded = decode_message(decode_dynamic_bytes(log.get("data", "0x")))
    if compute_hash:
        decoded["message_id"] = message_id(decoded["raw_hex"])
    decoded.update({
        "transaction_hash": normalize(log.get("transactionHash")),
        "block_hash": normalize(log.get("blockHash")),
        "block_number": int(log["blockNumber"], 16) if log.get("blockNumber") else None,
        "log_index": int(log["logIndex"], 16) if log.get("logIndex") else None,
        "contract": normalize(log.get("address")),
    })
    return decoded


def verify_dispatch(log: dict, expected: dict) -> dict:
    try:
        identity = inspect_dispatch(log)
    except Exception as exc:
        return {"valid": False, "reason": f"decode:{type(exc).__name__}:{exc}"}
    checks = {
        "nonce": identity["nonce"] == int(expected["nonce"]),
        "message_id": identity["message_id"] == normalize(expected["msg_id"]),
        "tx_hash": identity["transaction_hash"] == normalize(expected["origin_tx_hash"]),
        "block_hash": identity["block_hash"] == normalize(expected["origin_block_hash"]),
        "mailbox": identity["contract"] == normalize(expected["origin_mailbox"]),
        "origin": identity["origin"] == int(expected["origin_domain_id"]),
    }
    return {"valid": all(checks.values()), "checks": checks, "identity": identity}

