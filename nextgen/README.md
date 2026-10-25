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

### Full-resolution 3DGS with CPU camera offload

Graphdeco normally keeps every training image, mask and inverse-depth map on the
CUDA device. This can exhaust a 24 GiB GPU before optimization starts for a
large set of native 4K views. Set `NG_GS_DATA_DEVICE=cpu` to retain those camera
tensors in system RAM while Graphdeco copies only the sampled camera tensors to
CUDA during each training iteration. This does **not** lower image resolution or
split training into disjoint camera batches; all cameras remain available to the
normal random-view training loop.

For example, on a workstation with 128 GB system RAM and an RTX 3090, a guarded
full-resolution run can be launched with:

```bash
NG_DOCKER_MEMORY=100g \
NG_DOCKER_MEMORY_SWAP=100g \
NG_GS_ITERS=30000 \
NG_GS_RESOLUTION=1 \
NG_GS_DATA_DEVICE=cpu \
NG_GPU_MEMORY_FRACTION=0.88 \
./run_nextgen.sh train retail_competition_160
```

`NG_DOCKER_MEMORY=100g` is a hard container memory ceiling. Keeping
`NG_DOCKER_MEMORY_SWAP` equal to it prevents the container from gaining an
additional swap allowance. `NG_GPU_MEMORY_FRACTION` independently caps the
PyTorch CUDA allocator, so CPU RAM and VRAM remain bounded separately.

The default remains `NG_GS_DATA_DEVICE=cuda` for smaller datasets.

## Interactive localhost viewer

NextGen includes a self-contained browser viewer for the trained Graphdeco
Gaussian PLY. It also exposes the ordinary rendered PNGs in a gallery, which is
useful over a slower VPN before downloading the much larger Gaussian scene.

The viewer bundle is committed under `nextgen/viewer/dist`, so the college
server does **not** need Node/npm. The existing NextGen Docker image serves the
static app with Python and mounts the chosen scene read-only.

Start it in the background:

```bash
./run_nextgen.sh viewer-bg frames_omega_test
```

The wrapper auto-detects the latest `iteration_*` directory and prints a URL
similar to:

```text
http://127.0.0.1:8080/viewer/?scene=frames_omega_test&iter=10000
```

The Docker port is deliberately published on server `127.0.0.1` only. When the
scene lives on a remote college machine, forward the port from your laptop:

```bash
ssh -L 8080:127.0.0.1:8080 <college-server>
```

Then open the printed `http://127.0.0.1:8080/viewer/...` URL in the **laptop** browser.
VS Code Remote SSH users can instead forward port 8080 from the **Ports** panel.

Lifecycle commands:

```bash
./run_nextgen.sh viewer-bg <scene>   # background
./run_nextgen.sh viewer <scene>      # foreground
./run_nextgen.sh viewer-logs         # follow server logs
./run_nextgen.sh viewer-stop         # stop/remove viewer container
```

Use another local port when 8080 is occupied:

```bash
NG_VIEWER_PORT=8081 ./run_nextgen.sh viewer-bg frames_omega_test
```

Force a particular trained iteration if a scene contains several checkpoints:

```bash
NG_VIEWER_ITER=30000 ./run_nextgen.sh viewer-bg my_scene
```

The interactive 3D button loads
`06_gs_model/point_cloud/iteration_<N>/point_cloud.ply` directly. Large Gaussian
PLYs can be hundreds of MB, so the viewer intentionally does not fetch them until
you click **Load interactive 3D**. The **Renders** panel is much lighter-weight.

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
