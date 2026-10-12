#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Namma-IITB container

Commands:
  help
  smoke                         CPU-safe install/integration smoke test
  check                         NVIDIA GPU + CUDA preflight
  inspect <video>               Show codec/FPS/resolution/duration
  slam <video> [name]           RGB video -> MASt3R-SLAM
  convert <name> [sequence]     MASt3R output -> 3DGS dataset
  train <name>                  Train Graphdeco 3DGS
  render <name>                 Render trained views
  pipeline <video> [name]       slam -> convert -> train -> render
  shell                         Interactive shell

Inputs live under /workspace/data and persistent outputs under /workspace/outputs.

Tuning environment variables:
  SUBSAMPLE=2
  FOV_X=60
  MAX_POINTS=300000
  SOR_NEIGHBORS=20
  SOR_STD_RATIO=1.25
  GS_ITERS=7000
  GS_RESOLUTION=1
EOF
}

resolve_video() {
  local v="$1"
  if [[ -f "$v" ]]; then
    realpath "$v"
  elif [[ -f "/workspace/data/$v" ]]; then
    realpath "/workspace/data/$v"
  else
    echo "ERROR: video not found: $v" >&2
    echo "Place it in the project's data/ folder." >&2
    exit 2
  fi
}

case "${1:-help}" in
  help|-h|--help) usage ;;
  smoke) exec python /opt/namma/scripts/smoke_test.py ;;
  check) exec python /opt/namma/scripts/preflight.py ;;
  inspect)
    [[ $# -ge 2 ]] || { echo "Usage: inspect <video>" >&2; exit 2; }
    video="$(resolve_video "$2")"
    exec ffprobe -v error \
      -show_entries stream=index,codec_name,codec_type,width,height,r_frame_rate,avg_frame_rate,pix_fmt \
      -show_entries format=duration,size,bit_rate \
      -of default=noprint_wrappers=1 "$video"
    ;;
  slam) shift; exec /opt/namma/scripts/run_slam.sh "$@" ;;
  convert) shift; exec /opt/namma/scripts/convert.sh "$@" ;;
  train) shift; exec /opt/namma/scripts/train_3dgs.sh "$@" ;;
  render) shift; exec /opt/namma/scripts/render_3dgs.sh "$@" ;;
  pipeline) shift; exec /opt/namma/scripts/pipeline.sh "$@" ;;
  shell|bash) exec bash ;;
  *) echo "Unknown command: $1" >&2; usage >&2; exit 2 ;;
esac

