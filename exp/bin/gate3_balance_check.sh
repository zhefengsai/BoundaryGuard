#!/usr/bin/env bash
# Print Gate3 signer address + chain balances (never prints the key).
set -euo pipefail
ROOT="${1:-/data/boundaryguard}"
cd "$ROOT"
PY="$ROOT/.venv/bin/python3"
if [[ ! -x "$PY" ]]; then
  echo "ERROR: missing $PY — create with: python3 -m venv $ROOT/.venv && $ROOT/.venv/bin/pip install eth-account" >&2
  exit 1
fi
"$PY" - <<'PY'
import json, os, sys, urllib.request
from pathlib import Path
from eth_account import Account

def load_key():
    env = os.environ.get("HYP_DEFAULTSIGNER_KEY", "").strip()
    if env:
        return env
    for rel in ("work/configs/gate3_funded.key", "work/configs/gate3_ephemeral.key"):
        p = Path(rel)
        if p.exists():
            return p.read_text().strip()
    sys.exit("no key configured")

key = load_key()
if not key.startswith("0x"):
    key = "0x" + key
addr = Account.from_key(key).address
payload = json.dumps({
    "jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
    "params": [addr, "latest"],
}).encode()
for name, url in (
    ("optimism", "https://mainnet.optimism.io"),
    ("ethereum", "https://ethereum-rpc.publicnode.com"),
):
    try:
        req = urllib.request.Request(url, data=payload, headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            wei = int(json.loads(resp.read()).get("result") or "0x0", 16)
        print(f"{name}\t{addr}\t{wei}\t{wei/1e18:.6f} ETH")
    except Exception as exc:
        print(f"{name}\t{addr}\terror\t{exc}")
PY
