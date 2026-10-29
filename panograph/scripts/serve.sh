#!/usr/bin/env bash
set -Eeuo pipefail

SCENE="${1:-hindoor_unibs}"
PACKAGE="/workspace/outputs/$SCENE/04_sphr"
[[ -f "$PACKAGE/bootstrap.json" ]] || { echo "ERROR: SPHR package missing: $PACKAGE/bootstrap.json" >&2; exit 2; }

PUBLIC="/opt/sphr/public/datasets/panograph/$SCENE"
rm -rf "$PUBLIC"
mkdir -p "$PUBLIC/panos"
cp "$PACKAGE/bootstrap.json" "$PUBLIC/bootstrap.json"
cp "$PACKAGE/assets/panos/"* "$PUBLIC/panos/"
if compgen -G "$PACKAGE/assets/transitions/*.webm" > /dev/null; then
  mkdir -p "$PUBLIC/transitions"
  cp "$PACKAGE/assets/transitions/"*.webm "$PUBLIC/transitions/"
fi

echo "=== NAMMA PANOGRAPH: SPHR SERVER ==="
echo "Inside container: http://0.0.0.0:3000/?config=/datasets/panograph/$SCENE/bootstrap.json"
echo "Host/tunnel URL: http://127.0.0.1:${NP_VIEWER_PORT:-3000}/?config=/datasets/panograph/$SCENE/bootstrap.json"
echo

cd /opt/sphr
exec npm run start -- -H 0.0.0.0 -p 3000
