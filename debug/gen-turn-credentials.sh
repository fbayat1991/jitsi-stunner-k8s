#!/usr/bin/env bash
# Generate temporary TURN credentials (TURN REST API / STUNner "longterm" auth), valid for 1 hour.
#
# Usage:
#   ./debug/gen-turn-credentials.sh '<TURN_SHARED_SECRET>' [public-ip]
#
# Prints the username/password for the Trickle ICE test page and a ready-to-use
# turn:// URI (URL-encoded) for turncat.
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <TURN_SHARED_SECRET> [PUBLIC_IP]" >&2
  exit 1
fi

SECRET="$1"
HOST="${2:-<PUBLIC_IP>}"
TTL=3600

U="$(( $(date +%s) + TTL )):test"
P=$(printf '%s' "$U" | openssl dgst -sha1 -hmac "$SECRET" -binary | base64)

enc() { python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }

echo "username: $U"
echo "password: $P"
echo
echo "Trickle ICE URI : turn:${HOST}:3478?transport=udp"
echo "turncat URI     : turn://$(enc "$U"):$(enc "$P")@${HOST}:3478?transport=udp"
