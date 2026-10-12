#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: convert <run-name> [sequence-name]" >&2
    exit 2
fi

RUN_NAME="$1"
SOURCE="/workspace/outputs/mast3r/$RUN_NAME"

if [[ ! -d "$SOURCE" ]]; then
    echo "ERROR: MASt3R output not found: $SOURCE" >&2
    exit 2
fi

# ------------------------------------------------------------------
# Determine sequence name
# ------------------------------------------------------------------

if [[ $# -ge 2 ]]; then
    SEQ_NAME="$2"
else
    mapfile -t TRAJS < <(
        find "$SOURCE" \
            -maxdepth 1 \
            -type f \
            -name '*.txt' \
            -printf '%f\n' |
        sed 's/\.txt$//' |
        sort
    )

    if [[ ${#TRAJS[@]} -ne 1 ]]; then
        echo "ERROR: could not determine sequence name." >&2
        echo "Pass it explicitly:" >&2
        echo "  ./run.sh convert RUN_NAME SEQUENCE_NAME" >&2
        exit 2
    fi

    SEQ_NAME="${TRAJS[0]}"
fi

OUT="/workspace/outputs/gs_dataset/$RUN_NAME"

MAX_POINTS="${MAX_POINTS:-300000}"
SOR_NEIGHBORS="${SOR_NEIGHBORS:-20}"
SOR_STD_RATIO="${SOR_STD_RATIO:-1.25}"
FOV_X="${FOV_X:-60}"

rm -rf "$OUT"
mkdir -p "$OUT"

echo "============================================================"
echo " MASt3R -> 3DGS conversion"
echo "============================================================"
echo "Run:          $RUN_NAME"
echo "Sequence:     $SEQ_NAME"
echo "Max points:   $MAX_POINTS"

# ------------------------------------------------------------------
# CAMERA MODEL
#
# If CAMERA_PROFILE is set, automatically scale calibration to the
# resolution of the MASt3R keyframes.
#
# Otherwise fall back to FOV_X.
# ------------------------------------------------------------------

CAMERA_ARGS=()

if [[ -n "${CAMERA_PROFILE:-}" ]]; then

    PROFILE="/opt/namma/config/cameras/${CAMERA_PROFILE}.yaml"

    if [[ ! -f "$PROFILE" ]]; then
        echo "ERROR: camera profile does not exist:"
        echo "       $PROFILE"
        exit 3
    fi

    KEYFRAME_DIR="$SOURCE/keyframes/$SEQ_NAME"

    FIRST_IMAGE="$(
        find "$KEYFRAME_DIR" \
            -maxdepth 1 \
            -type f \
            \( -iname '*.png' -o -iname '*.jpg' -o -iname '*.jpeg' \) |
        sort |
        head -1
    )"

    if [[ -z "$FIRST_IMAGE" ]]; then
        echo "ERROR: no keyframe image found in $KEYFRAME_DIR"
        exit 3
    fi

    read -r FX FY CX CY IMG_W IMG_H REF_W REF_H <<< "$(
        python - "$PROFILE" "$FIRST_IMAGE" <<'PY'
import sys
import yaml
from PIL import Image

profile_path = sys.argv[1]
image_path = sys.argv[2]

with open(profile_path) as f:
    p = yaml.safe_load(f)

ref_w = float(p["width"])
ref_h = float(p["height"])

fx = float(p["fx"])
fy = float(p["fy"])
cx = float(p["cx"])
cy = float(p["cy"])

with Image.open(image_path) as im:
    w, h = im.size

sx = w / ref_w
sy = h / ref_h

fx2 = fx * sx
fy2 = fy * sy
cx2 = cx * sx
cy2 = cy * sy

print(
    fx2,
    fy2,
    cx2,
    cy2,
    w,
    h,
    int(ref_w),
    int(ref_h),
)
PY
    )"

    echo
    echo "Camera profile: $CAMERA_PROFILE"
    echo "Calibration:    ${REF_W}x${REF_H}"
    echo "Keyframes:      ${IMG_W}x${IMG_H}"
    echo
    echo "Scaled intrinsics:"
    printf "  fx = %.6f\n" "$FX"
    printf "  fy = %.6f\n" "$FY"
    printf "  cx = %.6f\n" "$CX"
    printf "  cy = %.6f\n" "$CY"

    CAMERA_ARGS=(
        --fx "$FX"
        --fy "$FY"
        --cx "$CX"
        --cy "$CY"
    )

else

    echo
    echo "No CAMERA_PROFILE supplied."
    echo "Falling back to FOV_X=$FOV_X degrees."

    CAMERA_ARGS=(
        --fov-x "$FOV_X"
    )

fi

# ------------------------------------------------------------------
# Export MASt3R -> COLMAP / Graphdeco dataset
# ------------------------------------------------------------------

cd /opt/SplatSLAM

python scripts/export_mast3r_to_3dgs.py \
    --source "$SOURCE" \
    --output "$OUT" \
    --scene "$SEQ_NAME" \
    --max-points "$MAX_POINTS" \
    "${CAMERA_ARGS[@]}"

SPARSE="$OUT/sparse/0"

# ------------------------------------------------------------------
# Initial PLY for SOR if exporter didn't make one
# ------------------------------------------------------------------

if [[ ! -s "$SPARSE/points3D.ply" ]]; then

    echo
    echo "[ INFO ] Creating temporary Graphdeco-compatible PLY..."

    python /opt/namma/scripts/points3d_txt_to_ply.py \
        "$SPARSE/points3D.txt" \
        "$SPARSE/points3D.ply"
fi

# ------------------------------------------------------------------
# Statistical outlier removal
# ------------------------------------------------------------------

python -m splatslam.sor_denoise \
    --sparse_dir "$SPARSE" \
    --nb_neighbors "$SOR_NEIGHBORS" \
    --std_ratio "$SOR_STD_RATIO"

# ------------------------------------------------------------------
# IMPORTANT:
# SOR may rewrite the PLY without Graphdeco normals.
# Always rebuild it AFTER SOR.
# ------------------------------------------------------------------

echo
echo "[ INFO ] Rebuilding final Graphdeco-compatible PLY after SOR..."

python /opt/namma/scripts/points3d_txt_to_ply.py \
    "$SPARSE/points3D.txt" \
    "$SPARSE/points3D.ply"

# ------------------------------------------------------------------
# Validate output
# ------------------------------------------------------------------

test -s "$SPARSE/cameras.txt"
test -s "$SPARSE/images.txt"
test -s "$SPARSE/points3D.txt"
test -s "$SPARSE/points3D.ply"

echo
echo "============================================================"
echo " 3DGS dataset complete"
echo "============================================================"
echo "Dataset: $OUT"
