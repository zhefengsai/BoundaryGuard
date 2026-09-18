#!/usr/bin/env bash
# Gate-3 testnet funded path (A4): OP Sepolia -> Sepolia via BoundaryGuard + official relayer.
#
# Usage on cloud:
#   bin/gate3_testnet_run.sh [seconds]
#   RESET_DB=1 INDEX_FROM=47041000 bin/gate3_testnet_run.sh 900
set -euo pipefail
ROOT=/data/boundaryguard
cd "$ROOT"
mkdir -p work/logs work/configs work/data
PY_VENV="$ROOT/.venv/bin/python3"
PY=python3
[[ -x "$PY_VENV" ]] && PY="$PY_VENV"

SECONDS_RUN="${1:-600}"
KEY_FILE="work/configs/gate3_funded.key"

if [[ -z "${HYP_DEFAULTSIGNER_KEY:-}" ]]; then
  if [[ -f "$KEY_FILE" ]]; then
    export HYP_DEFAULTSIGNER_KEY="$(tr -d ' \n' < "$KEY_FILE")"
  else
    echo "ERROR: set HYP_DEFAULTSIGNER_KEY or create $KEY_FILE" >&2
    exit 2
  fi
fi

PRIOR_TX="${PRIOR_DISPATCH_TX:-0xc70de8b2803f869ebb526a77e65d8c3420dbe11f484b978a1ac05c923e5b9eb2}"
RECIPIENT="${TEST_RECIPIENT:-0x0f0eaEd6Cea78b0950C27759f9E3c2B4369F864b}"
INDEX_FROM="${INDEX_FROM:-47041000}"

EXTRA=()
if [[ "${RESET_DB:-0}" == "1" ]]; then
  EXTRA+=(--reset-db)
fi

echo "start $(date -u +%Y-%m-%dT%H:%M:%SZ) seconds=$SECONDS_RUN index_from=$INDEX_FROM" | tee -a work/logs/gate3_testnet.log
"$PY" work/relayer_official_gate3_testnet.py \
  --source optimismsepolia \
  --dest sepolia \
  --seconds "$SECONDS_RUN" \
  --skip-pull \
  --gas-limit 500000 \
  --recipient "$RECIPIENT" \
  --prior-dispatch-tx "$PRIOR_TX" \
  --index-from "$INDEX_FROM" \
  "${EXTRA[@]}" \
  >> work/logs/gate3_testnet.log 2>&1
echo "done $(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee -a work/logs/gate3_testnet.log
