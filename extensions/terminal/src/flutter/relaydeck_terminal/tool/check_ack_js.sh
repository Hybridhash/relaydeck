#!/bin/sh
# Web-compiled ACK encoder check (see tool/ack_js_probe.dart).
# Usage: sh tool/check_ack_js.sh <path-to-dart> <path-to-node>
# Compiles the pure-Dart encoder with dart2js and executes it under
# node, failing on any byte mismatch. Exits nonzero on failure.
set -eu
DART_BIN="$1"
NODE_BIN="$2"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$(mktemp -d)/ack_js_probe.js"
"$DART_BIN" compile js -o "$OUT" "$HERE/ack_js_probe.dart" >/dev/null
GOT="$("$NODE_BIN" "$OUT")"
WANT="0:ff0000000000000000
52:ff0000000000000034
4294967295:ff00000000ffffffff
4294967296:ff0000000100000000
4294967297:ff0000000100000001
9007199254740991:ff001fffffffffffff
9007199254740992:ff001fffffffffffff
-1:ff0000000000000000"
if [ "$GOT" = "$WANT" ]; then
  echo "ack_js: dart2js encoder output matches (${GOT:+8 vectors})"
else
  echo "ack_js MISMATCH"
  echo "--- want ---"; printf '%s\n' "$WANT"
  echo "--- got ---"; printf '%s\n' "$GOT"
  exit 1
fi
