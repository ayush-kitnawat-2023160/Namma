#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Namma NextGen reconstruction

Commands:
  help
  check                              Check NextGen dependencies/GPU
  inspect <video>                    Show input video metadata
  calibrate <video> [scene]          RGB-only GeoCalib shared-intrinsic calibration
  prepare <video> [scene]            Calibrate/undistort + select useful views
  vggt <scene>                       VGGT-Omega camera/depth inference
  sfm <scene>                        SuperPoint + LightGlue + COLMAP/BA
  fuse <scene>                       Multi-view depth verification + surface fusion
  gs-dataset <scene>                 Build depth-regularized Graphdeco dataset
  train <scene>                      Train depth-regularized 3DGS
  render <scene>                     Render trained views
  mesh-truth <scene>                 Cross-view-verified TSDF geometry mesh
  mesh-pretty <scene>                Same TSDF geometry with multiview 4K colors
  mesh <scene>                       Build truth + presentation meshes
  sugar <scene>                      Host wrapper: SuGaR mesh from trained 3DGS
  mesh-all <scene>                   Host wrapper: truth + presentation + SuGaR
  viewer <scene>                     Serve interactive Gaussian viewer
  viewer-bg <scene>                  Start viewer in background (host wrapper)
  viewer-logs                        Follow background viewer logs (host wrapper)
  viewer-stop                        Stop background viewer (host wrapper)
  pipeline <video> [scene]           Run all NextGen stages
  fix-perms                          Fix host ownership of outputs
  shell                              Interactive container shell

Important environment variables:
  NG_CAMERA_PROFILE=advio15
  NG_CALIB_VIEWS=20
  NG_CALIB_MODEL=radial
  NG_TARGET_VIEWS=160
  NG_MAX_CANDIDATES=1200
  NG_VGGT_CHECKPOINT=/workspace/checkpoints/VGGT-Omega-1B-512.pt
  NG_VGGT_RESOLUTION=512
  NG_VGGT_MODE=max_size
  NG_TEMPORAL_WINDOW=5
  NG_VISUAL_NEIGHBORS=8
  NG_GPU_MEMORY_FRACTION=0.90
  NG_MIN_FREE_VRAM_GB=18
  NG_MAX_GPU_TEMP_C=82
  NG_FUSION_NEIGHBORS=6
  NG_FUSION_REL_DEPTH=0.04
  NG_FUSION_MIN_SUPPORT=1
  NG_FUSION_PIXEL_STRIDE=3
  NG_GS_ITERS=15000
  NG_GS_RESOLUTION=1
  NG_GS_DATA_DEVICE=cpu|cuda
  NG_TSDF_NEIGHBORS=8
  NG_TSDF_REL_DEPTH=0.03
  NG_TSDF_MIN_SUPPORT=2
  NG_TSDF_VOXEL_SCALE=1.0
  NG_TSDF_TRUNC_VOXELS=5.0
  NG_PRESENTATION_TARGET_TRIANGLES=1000000
  NG_SUGAR_REGULARIZATION=dn_consistency
  NG_SUGAR_REFINEMENT_TIME=medium
  NG_SUGAR_SQUARE_SIZE=10
EOF
}

resolve_video() {
  local value="$1"
  if [[ -f "$value" ]]; then
    realpath "$value"
  elif [[ -f "/workspace/data/$value" ]]; then
    realpath "/workspace/data/$value"
  else
    echo "ERROR: video not found: $value" >&2
    exit 2
  fi
}

case "${1:-help}" in
  help|-h|--help)
    usage
    ;;
  check)
    exec python /opt/namma-nextgen/scripts/preflight.py
    ;;
  inspect)
    [[ $# -ge 2 ]] || { echo "Usage: inspect <video>" >&2; exit 2; }
    video="$(resolve_video "$2")"
    exec ffprobe -v error \
      -show_entries stream=codec_name,width,height,r_frame_rate,avg_frame_rate,pix_fmt \
      -show_entries format=duration,size,bit_rate \
      -of default=noprint_wrappers=1 "$video"
    ;;
  calibrate)
    shift
    exec /opt/namma-nextgen/scripts/calibrate.sh "$@"
    ;;
  prepare)
    shift
    exec /opt/namma-nextgen/scripts/prepare.sh "$@"
    ;;
  vggt)
    shift
    exec /opt/namma-nextgen/scripts/vggt.sh "$@"
    ;;
  sfm)
    shift
    exec /opt/namma-nextgen/scripts/sfm.sh "$@"
    ;;
  fuse)
    shift
    exec /opt/namma-nextgen/scripts/fuse_geometry.sh "$@"
    ;;
  gs-dataset)
    shift
    exec /opt/namma-nextgen/scripts/build_gs_dataset.sh "$@"
    ;;
  train)
    shift
    exec /opt/namma-nextgen/scripts/train_gs.sh "$@"
    ;;
  render)
    shift
    exec /opt/namma-nextgen/scripts/render_gs.sh "$@"
    ;;
  mesh-truth)
    shift
    exec /opt/namma-nextgen/scripts/mesh_truth.sh "$@"
    ;;
  mesh-pretty)
    shift
    exec /opt/namma-nextgen/scripts/mesh_presentation.sh "$@"
    ;;
  mesh)
    shift
    exec /opt/namma-nextgen/scripts/mesh_all.sh "$@"
    ;;
  viewer)
    [[ -d /srv/viewer ]] || { echo "ERROR: viewer bundle is not mounted" >&2; exit 2; }
    [[ -d /srv/scene ]] || { echo "ERROR: scene output is not mounted" >&2; exit 2; }
    echo "=== NAMMA NEXTGEN: INTERACTIVE VIEWER ==="
    echo "Serving on container port 8080"
    exec python -m http.server 8080 --bind 0.0.0.0 --directory /srv
    ;;
  pipeline)
    shift
    exec /opt/namma-nextgen/scripts/pipeline.sh "$@"
    ;;
  fix-perms)
    uid="${HOST_UID:-1000}"
    gid="${HOST_GID:-1000}"
    chown -R "${uid}:${gid}" /workspace/outputs
    echo "Fixed output ownership to ${uid}:${gid}."
    ;;
  shell|bash)
    exec bash
    ;;
  *)
    echo "Unknown command: $1" >&2
    usage >&2
    exit 2
    ;;
esac
