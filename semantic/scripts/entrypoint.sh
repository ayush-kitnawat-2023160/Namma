#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Namma Semantic Digital-Twin extension

This pipeline is intentionally isolated from NextGen. It reads
/workspace/nextgen/<scene> read-only and writes only to
/workspace/semantic/<scene>.

Commands:
  help
  check                         Check semantic/geometry dependencies
  source <scene>                Validate NextGen artifacts + write source manifest
  status <scene>                Show semantic-stage status for a scene
  segment <scene> [options]     2D stuff + object-instance masks
  semantic-cloud <scene>        Fuse masks + depth + BA poses in 3D     [planned]
  layout <scene>                Fit floor/wall/ceiling geometry         [planned]
  object-mesh <scene>           Per-instance masked TSDF meshes         [planned]
  texture <scene>               Bake observed RGB onto meshes           [planned]
  scene <scene>                 Assemble GLB/USD + scene graph          [planned]
  viewer <scene>                First-person scene package              [planned]
  pipeline <scene>              Full semantic extension                 [planned]
  fix-perms                     Fix host ownership of semantic outputs
  shell                         Interactive container shell

Segmentation options:
  --limit N                     Evenly sample N views (quick preview)
  --overwrite                   Replace an existing 01_segmentation stage

Useful segmentation environment variables:
  NS_STUFF_MODEL
  NS_DETECTOR_MODEL
  NS_SAM2_MODEL
  NS_BOX_THRESHOLD
  NS_TEXT_THRESHOLD
  NS_NMS_IOU
  NS_MIN_MASK_AREA
  NS_SEGMENT_LIMIT
  NS_SEGMENT_OVERWRITE=1
EOF
}

not_implemented() {
  local stage="$1"
  echo "Stage '$stage' is scaffolded but not implemented yet." >&2
  echo "Run 'source <scene>' first; then implement/test stages one at a time." >&2
  exit 3
}

case "${1:-help}" in
  help|-h|--help)
    usage
    ;;
  check)
    exec python /opt/namma-semantic/scripts/preflight.py
    ;;
  source)
    [[ $# -eq 2 ]] || { echo "Usage: source <scene>" >&2; exit 2; }
    exec python /opt/namma-semantic/scripts/source_manifest.py --scene "$2"
    ;;
  status)
    [[ $# -eq 2 ]] || { echo "Usage: status <scene>" >&2; exit 2; }
    exec python /opt/namma-semantic/scripts/status.py --scene "$2"
    ;;
  segment)
    shift
    exec python /opt/namma-semantic/scripts/segment_scene.py "$@"
    ;;
  semantic-cloud|layout|object-mesh|texture|scene|viewer|pipeline)
    not_implemented "$1"
    ;;
  fix-perms)
    uid="${HOST_UID:-1000}"
    gid="${HOST_GID:-1000}"
    chown -R "${uid}:${gid}" /workspace/semantic /workspace/hf_cache
    echo "Fixed semantic output/cache ownership to ${uid}:${gid}."
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
