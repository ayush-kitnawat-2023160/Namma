#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Namma PanoGraph — modern RGB-only panoramic tour pipeline

Commands:
  help
  check                              Check CPU/container dependencies
  gpu-check                          Check CUDA and free VRAM guard
  fetch                              Download HIndoor-8K unibs RGB only
  models                             Download PanoVGGT checkpoint
  prepare [scene]                    Validate/compress panoramas for viewer
  reconstruct [scene]                Pairwise PanoVGGT pose estimation
  graph [scene]                      Build station graph
  viewer [scene]                     Export SPHR bootstrap/package
  validate [scene]                   Validate RGB-only data + graph + viewer package
  pipeline [scene]                   Fetch + model + prepare + reconstruct + graph + viewer
  status [scene]
  serve [scene]                      Run SPHR on container port 3000
  fix-perms
  shell

Build image from host wrapper:
  ./run_panograph.sh build

Default scene name:
  hindoor_unibs

Safety variables:
  NP_GPU_DEVICE=0
  NP_GPU_MEMORY_FRACTION=0.82        PyTorch hard per-process fraction cap
  NP_GPU_MIN_FREE_MB=18000           refuse inference if GPU is busy
  NP_RAM_LIMIT=32g                    Docker cgroup RAM limit
  NP_SHM_SIZE=16g

Viewer variables:
  NP_VIEWER_PORT=3000
  NP_VIEWER_MAX_WIDTH=4096
  NP_VIEWER_WEBP_QUALITY=88

No host Python/Conda/Node environment is required. All work runs in Docker.
EOF
}

case "${1:-help}" in
  help|-h|--help)
    usage
    ;;
  check)
    exec python3 /opt/namma-panograph/scripts/check.py
    ;;
  gpu-check)
    exec python3 /opt/namma-panograph/scripts/gpu_check.py
    ;;
  fetch)
    exec python3 /opt/namma-panograph/scripts/fetch_data.py
    ;;
  models)
    exec python3 /opt/namma-panograph/scripts/fetch_models.py
    ;;
  prepare)
    shift
    exec python3 /opt/namma-panograph/scripts/prepare.py "$@"
    ;;
  reconstruct)
    shift
    exec python3 /opt/namma-panograph/scripts/panovggt_pairwise.py "$@"
    ;;
  graph)
    shift
    exec python3 /opt/namma-panograph/scripts/build_graph.py "$@"
    ;;
  viewer)
    shift
    exec python3 /opt/namma-panograph/scripts/export_sphr.py "$@"
    ;;
  validate)
    shift
    exec python3 /opt/namma-panograph/scripts/validate.py "$@"
    ;;
  pipeline)
    shift
    exec /opt/namma-panograph/scripts/pipeline.sh "$@"
    ;;
  status)
    shift
    exec python3 /opt/namma-panograph/scripts/status.py "$@"
    ;;
  serve)
    shift
    exec /opt/namma-panograph/scripts/serve.sh "$@"
    ;;
  fix-perms)
    uid="${HOST_UID:-1000}"
    gid="${HOST_GID:-1000}"
    chown -R "$uid:$gid" /workspace/outputs /workspace/data /workspace/checkpoints /workspace/hf_cache || true
    echo "Fixed PanoGraph ownership to $uid:$gid."
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
