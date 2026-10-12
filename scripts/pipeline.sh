#!/usr/bin/env bash
set -Eeuo pipefail

PIPELINE_START_EPOCH="$(date +%s)"
CURRENT_STAGE="startup"
CURRENT_STAGE_START="$PIPELINE_START_EPOCH"

format_duration() {
  local total="${1:-0}"
  local h=$(( total / 3600 ))
  local m=$(( (total % 3600) / 60 ))
  local s=$(( total % 60 ))
  printf '%02dh:%02dm:%02ds' "$h" "$m" "$s"
}

stage_start() {
  CURRENT_STAGE="$1"
  CURRENT_STAGE_START="$(date +%s)"
  echo
  echo "------------------------------------------------------------"
  echo " STARTING: $CURRENT_STAGE"
  echo "------------------------------------------------------------"
}

stage_done() {
  local now elapsed
  now="$(date +%s)"
  elapsed=$(( now - CURRENT_STAGE_START ))
  echo "------------------------------------------------------------"
  echo " FINISHED: $CURRENT_STAGE in $(format_duration "$elapsed")"
  echo "------------------------------------------------------------"
}

on_error() {
  local exit_code=$?
  local now total stage_elapsed
  now="$(date +%s)"
  total=$(( now - PIPELINE_START_EPOCH ))
  stage_elapsed=$(( now - CURRENT_STAGE_START ))
  echo >&2
  echo "============================================================" >&2
  echo " PIPELINE FAILED" >&2
  echo " Stage:       $CURRENT_STAGE" >&2
  echo " Stage time:  $(format_duration "$stage_elapsed")" >&2
  echo " Total time:  $(format_duration "$total")" >&2
  echo " Exit status: $exit_code" >&2
  echo "============================================================" >&2
  exit "$exit_code"
}

trap on_error ERR

[[ $# -ge 1 ]] || { echo "Usage: pipeline <video> [name]" >&2; exit 2; }
INPUT="$1"
if [[ -f "$INPUT" ]]; then VIDEO="$(realpath "$INPUT")"
elif [[ -f "/workspace/data/$INPUT" ]]; then VIDEO="$(realpath "/workspace/data/$INPUT")"
else echo "Video not found: $INPUT" >&2; exit 2; fi

SEQ_NAME="$(basename "$VIDEO")"; SEQ_NAME="${SEQ_NAME%.*}"
NAME="${2:-$SEQ_NAME}"

HQ_ENABLED="${HQ:-0}"
TRAIN_NAME="$NAME"

if [[ "$HQ_ENABLED" == "1" || "$HQ_ENABLED" == "true" || "$HQ_ENABLED" == "TRUE" ]]; then
  [[ -n "${CAMERA_PROFILE:-}" ]] || {
    echo "ERROR: HQ=1 requires CAMERA_PROFILE=<profile-name>." >&2
    echo "Example: CAMERA_PROFILE=advio15 HQ=1 ./run.sh pipeline frames.mov library" >&2
    exit 2
  }

  TRAIN_NAME="${HQ_RUN:-${NAME}_hq}"
  [[ "$TRAIN_NAME" != "$NAME" ]] || {
    echo "ERROR: HQ_RUN must differ from the base run name '$NAME'." >&2
    exit 2
  }
fi

echo "=== Namma full RGB-only pipeline ==="
echo "video=$VIDEO name=$NAME subsample=${SUBSAMPLE:-2} max_points=${MAX_POINTS:-300000} gs_iters=${GS_ITERS:-7000}"
if [[ -n "${CAMERA_PROFILE:-}" ]]; then
  echo "camera_profile=$CAMERA_PROFILE"
else
  echo "camera_profile=<none> (conversion fallback FOV_X=${FOV_X:-60})"
fi
echo "hq=$HQ_ENABLED"

stage_start "MASt3R-SLAM"
/opt/namma/scripts/run_slam.sh "$VIDEO" "$NAME"
stage_done

stage_start "MASt3R -> 3DGS conversion"
/opt/namma/scripts/convert.sh "$NAME" "$SEQ_NAME"
stage_done

if [[ "$HQ_ENABLED" == "1" || "$HQ_ENABLED" == "true" || "$HQ_ENABLED" == "TRUE" ]]; then
  stage_start "Original-resolution undistorted GS dataset"
  rm -rf "/workspace/outputs/gs_dataset/$TRAIN_NAME"
  /opt/namma/scripts/make_hq_dataset.sh "$NAME" "$VIDEO" "$TRAIN_NAME" "$SEQ_NAME"
  stage_done
fi

stage_start "3DGS training"
/opt/namma/scripts/train_3dgs.sh "$TRAIN_NAME"
stage_done

stage_start "3DGS rendering"
/opt/namma/scripts/render_3dgs.sh "$TRAIN_NAME"
stage_done

PIPELINE_END_EPOCH="$(date +%s)"
PIPELINE_TOTAL_SECONDS=$(( PIPELINE_END_EPOCH - PIPELINE_START_EPOCH ))

echo "=== COMPLETE ==="
echo "Final PLY: /workspace/outputs/gs_model/$TRAIN_NAME/point_cloud/iteration_${GS_ITERS:-7000}/point_cloud.ply"
echo "Total pipeline time: $(format_duration "$PIPELINE_TOTAL_SECONDS")"
