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
  -> ~160 selected views (VRAM guard refuses unsafe batches)
  -> VGGT-Omega camera/depth/geometry prediction
  -> exact pixel-aligned Omega RGB/depth/confidence retained
  -> SuperPoint pooled retrieval + temporal pairing + LightGlue correspondences
  -> VGGT pose initialization + LightGlue triangulation + global COLMAP BA
  -> cross-view Omega depth verification
  -> confidence/support-weighted voxel/surfel surface fusion + normals
  -> fused surface aligned to the COLMAP reconstruction
  -> Omega inverse-depth priors retained for training regularization
  -> depth-regularized Graphdeco 3DGS
  -> antialiasing + exposure compensation
  -> high-quality Gaussian PLY
```

The important change from the first NextGen prototype is that the raw union of
VGGT-Omega depth points is no longer used directly as the Gaussian initializer.
Every sampled surface point must agree with at least one other useful view by
default, then supported observations are fused into one surface. This removes a
large class of duplicated walls, floaters and side-view noise before splat
optimization gets a chance to hide them with appearance.

Graphdeco remains the stable renderer baseline. The fused surface is deliberately
kept as a separate persistent stage so OMeGa/2DGS or another surface-bound splat
backend can be A/B tested later without rerunning Omega or SfM.

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
NG_TARGET_VIEWS=160 \
./run_nextgen.sh prepare frames.mov advio_nextgen
```

For an arbitrary RGB phone video, omit `NG_CAMERA_PROFILE`. NextGen then samples
sharp frames across the video and runs GeoCalib with shared intrinsics before
undistorting/selecting reconstruction views:

```bash
NG_CALIB_VIEWS=20 \
NG_TARGET_VIEWS=160 \
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

The current persistent stage tree is:

```text
00_calibration/
01_frames/
02_vggt/
03_sfm/
04_geometry/       # cross-view verified fused surface + normals/support metadata
05_gs_dataset/
06_gs_model/
07_renders/
```

Useful individual commands:

```bash
./run_nextgen.sh vggt <scene>
./run_nextgen.sh sfm <scene>
./run_nextgen.sh fuse <scene>
./run_nextgen.sh gs-dataset <scene>
./run_nextgen.sh train <scene>
./run_nextgen.sh render <scene>
```

## Fail-safe Docker / GPU defaults

NextGen is intentionally configured so an oversized reconstruction should kill
or fail the container/process rather than exhaust the workstation:

- private IPC namespace (no `--ipc=host`)
- container RAM defaults to 70% of host RAM, capped at 32 GiB
- `memory-swap` defaults to the same value, preventing a swap-storm fallback
- `/dev/shm` defaults to 6 GiB
- PID limit: 4096
- bounded local Docker logs (20 MiB x 3)
- at least 25 GiB free project-disk space required before heavy stages
- at least 18 GiB free VRAM required before GPU-heavy stages
- GPU temperature must be below 82 C before a heavy stage starts
- PyTorch allocator is capped at 90% of visible VRAM
- VGGT-Omega estimates batch memory from the number of views and refuses batches
  above its safety budget instead of intentionally driving the GPU into OOM
- VGGT, SfM and 3DGS model outputs are written to temporary directories and only
  atomically replace the previous valid result after successful completion

These can be overridden deliberately, for example:

```bash
NG_DOCKER_MEMORY=40g \
NG_DOCKER_MEMORY_SWAP=40g \
NG_GPU_MEMORY_FRACTION=0.92 \
NG_MIN_FREE_VRAM_GB=20 \
./run_nextgen.sh pipeline frames.mov final_scene
```

Do not raise these limits just to make an unstable run continue. First reduce
`NG_TARGET_VIEWS`, `NG_FUSION_MAX_POINTS`, or the 3DGS resolution/iterations.

## Geometry-fusion controls

The initial conservative defaults are:

```text
NG_FUSION_NEIGHBORS=6
NG_FUSION_REL_DEPTH=0.04
NG_FUSION_MIN_SUPPORT=1
NG_FUSION_PIXEL_STRIDE=3
NG_FUSION_CONFIDENCE_PERCENTILE=30
NG_FUSION_MAX_POINTS=2000000
```

`NG_FUSION_MIN_SUPPORT=1` means a point must be supported by at least one *other*
view. Once a scene has good overlap, test `2` for stricter cleanup. Likewise,
reduce `NG_FUSION_REL_DEPTH` from `0.04` toward `0.02-0.03` only after checking
that thin structures and object boundaries are not being erased.
