#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: hq <run-name> <video> [out-name] [sequence]" >&2
  echo "Requires CAMERA_PROFILE=<profile-name>." >&2
  exit 2
fi

RUN_NAME="$1"
INPUT="$2"
OUT_NAME="${3:-${RUN_NAME}_hq}"
SEQ_NAME="${4:-}"

if [[ -f "$INPUT" ]]; then
  VIDEO="$(realpath "$INPUT")"
elif [[ -f "/workspace/data/$INPUT" ]]; then
  VIDEO="$(realpath "/workspace/data/$INPUT")"
else
  echo "ERROR: video not found: $INPUT" >&2
  exit 2
fi

if [[ -z "${CAMERA_PROFILE:-}" ]]; then
  echo "ERROR: HQ dataset generation requires CAMERA_PROFILE=<profile-name>." >&2
  exit 2
fi

PROFILE="/opt/namma/config/cameras/${CAMERA_PROFILE}.yaml"
if [[ ! -f "$PROFILE" ]]; then
  echo "ERROR: camera profile does not exist: $PROFILE" >&2
  exit 3
fi

ARGS=(
  --run "$RUN_NAME"
  --out-run "$OUT_NAME"
  --video "$VIDEO"
  --camera "$PROFILE"
)

[[ -n "$SEQ_NAME" ]] && ARGS+=(--sequence "$SEQ_NAME")

exec python /opt/namma/scripts/make_hq_dataset.py "${ARGS[@]}"
