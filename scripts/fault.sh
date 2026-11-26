#!/usr/bin/env bash
# Usage: bash scripts/fault.sh <fault-name>
# Requires FAULTS=1 in .env and the ./faults bind mount (default in compose).
set -euo pipefail
name="${1:?fault name required}"
case "$name" in
  after_commit_before_response|publisher_before_send|after_external_before_commit) ;;
  *) echo "unknown fault: $name" >&2; exit 2 ;;
esac
mkdir -p faults
touch "faults/$name"
echo "Flag created: faults/$name"
echo "The next matching operation will log fault.hit and exit with code 137."
