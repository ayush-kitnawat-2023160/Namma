# Namma NextGen reconstruction branch

This directory is intentionally isolated from the working MASt3R pipeline on
`main`. The original `run.sh`, Docker image, outputs and MASt3R workflow remain
available unchanged. NextGen uses its own image (`namma-nextgen:latest`), runner
(`run_nextgen.sh`) and output tree (`outputs/nextgen/<scene>`).

## Target architecture

```text
RGB video
  -> known camera profile OR GeoCalib shared-intrinsic RGB calibration
  -> undistortion to a common pinhole camera
  -> blur/exposure/duplicate filtering
  -> ~100-150 selected views
  -> VGGT-Omega camera/depth/geometry prediction
  -> SuperPoint + LightGlue correspondences
  -> VGGT pose initialization + LightGlue triangulation + global COLMAP BA
  -> VGGT inverse-depth priors aligned to the COLMAP reconstruction
  -> depth-regularized Graphdeco 3DGS
  -> antialiasing + exposure compensation
  -> high-quality Gaussian PLY
```

## Why this lives separately

The existing MASt3R pipeline is the stable baseline. NextGen is experimental and
may pull substantially different dependencies (VGGT-Omega, HLoc, LightGlue,
pycolmap). Keeping a second image and CLI lets us A/B test the same video without
changing the stable pipeline.

## Build

The base `namma-iitb:latest` image must already exist.

```bash
./build_nextgen.sh
```

## VGGT-Omega checkpoint

The official VGGT-Omega checkpoint is gated. Request/download the recommended
`VGGT-Omega-1B-512` checkpoint and place it locally at:

```text
nextgen/checkpoints/VGGT-Omega-1B-512.pt
```

Checkpoints are git-ignored and are mounted read-only into the container.

## First run

For a calibrated ADVIO sequence:

```bash
NG_CAMERA_PROFILE=advio15 \
NG_TARGET_VIEWS=120 \
./run_nextgen.sh prepare frames.mov advio_nextgen
```

For an arbitrary RGB phone video, omit `NG_CAMERA_PROFILE`. NextGen then samples
sharp frames across the video and runs GeoCalib with shared intrinsics before
undistorting/selecting reconstruction views:

```bash
NG_CALIB_VIEWS=20 \
NG_TARGET_VIEWS=120 \
./run_nextgen.sh prepare corridor.mp4 corridor_nextgen
```

The automatic estimate is written to
`outputs/nextgen/<scene>/00_calibration/camera.yaml`. GeoCalib assumes a centered
principal point; the downstream VGGT + LightGlue geometry is therefore used as a
consistency check rather than blindly trusting one monocular calibration estimate.

Once the VGGT checkpoint is present:

```bash
NG_CAMERA_PROFILE=advio15 \
NG_VGGT_CHECKPOINT=/workspace/checkpoints/VGGT-Omega-1B-512.pt \
./run_nextgen.sh pipeline frames.mov advio_nextgen
```

Every stage is persistent under `outputs/nextgen/advio_nextgen/`, so stages can
also be run individually for debugging and comparison.
