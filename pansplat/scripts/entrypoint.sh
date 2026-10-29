#!/usr/bin/env bash
set -Eeuo pipefail
cmd="${1:-help}"
case "$cmd" in
  help|-h|--help)
    cat <<'TXT'
Namma PanSplat — offline Gaussian transitions for PanoGraph

Commands:
  check
  gpu-check
  models [512|4k]
  edge <scene> <station_a> <station_b> [--profile 512|4k]
  render <scene> [--profile 512|4k] [--max-edges N]
  validate <scene>
  shell
TXT
    ;;
  check) exec python3 /opt/namma-pansplat/scripts/check.py ;;
  gpu-check) shift; exec python3 /opt/namma-pansplat/scripts/gpu_check.py "$@" ;;
  models) shift; exec python3 /opt/namma-pansplat/scripts/fetch_models.py "$@" ;;
  edge) shift; exec python3 /opt/namma-pansplat/scripts/render_edge.py "$@" ;;
  render) shift; exec python3 /opt/namma-pansplat/scripts/render_all.py "$@" ;;
  validate) shift; exec python3 /opt/namma-pansplat/scripts/validate.py "$@" ;;
  shell|bash) exec bash ;;
  *) echo "Unknown command: $cmd" >&2; exit 2 ;;
esac
