# Namma — RGB Video → MASt3R-SLAM → 3D Gaussian Splatting

For the RTX 3090 / 128 GB college server, see the new **[Precision reconstruction workflow](precision/README.md)**. It adds final-camera high-resolution stereo, geometric quality gates, bounded training, and an isolated output tree. Existing MASt3R, NextGen, semantic and panorama commands remain available. Do not run reconstruction or Docker builds on the laptop.

Portable, headless Docker project for the Namma Space Round-1 reconstruction experiment.

## Host requirements

Nothing from the ML stack is installed on the host. For GPU inference the host only needs:

1. Docker
2. NVIDIA driver
3. NVIDIA Container Toolkit (`docker run --gpus all ...` must work)

Python, Conda, CUDA Toolkit, MASt3R-SLAM, the checkpoints, SplatSLAM, Open3D and Graphdeco 3DGS all live inside the image.

## Pipeline

```text
single RGB video
  ↓
MASt3R-SLAM (headless)
  ├─ tracking / keyframes
  ├─ retrieval + loop closure
  ├─ global optimization
  └─ trajectory + dense colored PLY
  ↓
SplatSLAM bridge
  ├─ pose-convention conversion
  ├─ calibrated intrinsics when CAMERA_PROFILE is provided
  ├─ FOV fallback when no calibration is available
  ├─ point downsampling
  └─ SOR outlier cleanup
  ↓
optional HQ dataset stage
  ├─ recover original-resolution frames using corrected timestamps
  ├─ lens undistortion from the camera profile
  └─ update the COLMAP pinhole camera after rectification
  ↓
Graphdeco 3DGS
  ↓
final Gaussian PLY + rendered views
```

The MASt3R-SLAM viewer path is deliberately disabled/lazy-imported. This avoids fragile OpenGL/pyimgui dependencies and is the correct mode for an SSH compute node.

## Project layout

```text
Namma/
├── Dockerfile
├── build.sh
├── run.sh
├── config/
├── scripts/
├── data/               # put input videos here
└── outputs/
    ├── mast3r/
    ├── gs_dataset/
    ├── gs_model/
    ├── renders/
    └── logs/
```

All runtime paths are relative to the project root, so the folder can be copied anywhere on the college PC.

## First-time build

```bash
cd /wherever/Namma
./build.sh
```

This is a large build because it contains CUDA, PyTorch, checkpoints and compiled CUDA extensions.

`run.sh` mounts the local `scripts/` and `config/` directories read-only into the
container. After the first successful image build, ordinary script/config edits do
not require rebuilding the large Docker image; rebuild only when the Dockerfile or
container dependencies change.

Docker jobs run as root inside the container, so generated files can occasionally
become root-owned on the host. Repair ownership at any time with:

```bash
./run.sh fix-perms
```

## CPU-safe pseudo/smoke test

```bash
./run.sh smoke
```

This checks repository CLIs, compiled imports, video decoding and a synthetic MASt3R-output → COLMAP/3DGS conversion without needing a real GPU.

## GPU preflight on the college machine

```bash
./run.sh check
```

It prints the GPU, compute capability, total/free VRAM, verifies checkpoints and compiled CUDA modules, and executes a tiny CUDA operation.

## Input

Copy an RGB video to:

```text
data/library.mp4
```

Inspect it without GPU:

```bash
./run.sh inspect library.mp4
```

## Recommended first real run: SLAM only

```bash
./run.sh slam library.mp4
```

For a longer 30 FPS corridor, start more conservatively:

```bash
SUBSAMPLE=3 ./run.sh slam corridor.mp4 corridor
```

Expected persistent output:

```text
outputs/mast3r/library/
├── library.txt
├── library.ply
└── keyframes/library/*.png
```

Inspect the point cloud/trajectory before spending time training 3DGS.

## Convert to 3DGS input

```bash
./run.sh convert library
```

No camera profile is assumed automatically. For a known camera, pass a profile:

```bash
CAMERA_PROFILE=advio15 MAX_POINTS=500000 ./run.sh convert library
```

The converter scales the stored calibration to the MASt3R keyframe resolution. If
`CAMERA_PROFILE` is omitted, it falls back to `FOV_X=60` unless overridden.

Other defaults:

- `MAX_POINTS=300000`
- SOR: 20 neighbors, std ratio 1.25

You can override them from the host shell and `run.sh` forwards them into the container:

```bash
FOV_X=65 MAX_POINTS=250000 ./run.sh convert library
```

## Original-resolution undistorted GS dataset

For a calibrated camera, rebuild the converted dataset using original-resolution
video frames and the profile's distortion coefficients:

```bash
CAMERA_PROFILE=advio15 \
./run.sh hq library frames.mov library_hq
```

This keeps the MASt3R poses/geometry, extracts the original video frame for every
keyframe timestamp, undistorts it, and writes the corresponding rectified PINHOLE
camera to the new dataset. Sequence names are discovered automatically when there
is only one MASt3R trajectory; an explicit sequence can be supplied as the fourth
argument when necessary.

Train/render the HQ dataset normally:

```bash
GS_ITERS=10000 ./run.sh train library_hq
GS_ITERS=10000 ./run.sh render library_hq
```

## Train 3DGS

```bash
./run.sh train library
```

The first validation run uses 7000 iterations. If it looks correct:

```bash
GS_ITERS=15000 ./run.sh train library
```

Final default result:

```text
outputs/gs_model/library/point_cloud/iteration_7000/point_cloud.ply
```

## Render

```bash
./run.sh render library
```

## One-command pipeline

Once SLAM-only has been validated:

```bash
./run.sh pipeline library.mp4
```

While the pipeline runs, it reports the elapsed time of each major stage
(MASt3R-SLAM, MASt3R-to-3DGS conversion, 3DGS training, and rendering). At the
end it prints the total end-to-end runtime. If a stage fails, the error summary
also shows the failed stage and elapsed time up to the failure.

For a longer corridor:

```bash
SUBSAMPLE=3 MAX_POINTS=300000 GS_ITERS=7000 \
./run.sh pipeline corridor.mp4 corridor
```

To include the original-resolution undistortion stage in the one-command pipeline:

```bash
CAMERA_PROFILE=advio15 HQ=1 MAX_POINTS=500000 GS_ITERS=10000 \
./run.sh pipeline frames.mov library
```

The HQ dataset/model defaults to `<name>_hq`; override it with `HQ_RUN=<name>`.

## 24 GB VRAM safety choices

- MASt3R stays at its native ~512 network-input regime.
- `SUBSAMPLE=2` by default to remove redundant 30 FPS frames.
- 300k points max are exported to 3DGS initially.
- 7k 3DGS iterations for the first run.
- PyTorch expandable CUDA allocations enabled.
- Docker uses `--ipc=host --shm-size=32g`.
- no GUI/viewer on the remote machine.
- `./run.sh check` warns if less than 16 GiB is currently free.

If memory is still tight, first use `SUBSAMPLE=3` or `4` and `MAX_POINTS=200000`.

## Camera calibration

Camera profiles live under `config/cameras/` and contain reference resolution,
`fx/fy/cx/cy`, and optional distortion coefficients. The project deliberately does
**not** default to any specific profile: this prevents a random phone video from
silently receiving ADVIO intrinsics.

When a profile is supplied, `convert` scales its intrinsics to the MASt3R keyframe
resolution. The `hq` stage additionally applies lens undistortion at the original
video resolution. If no calibration is available, `convert` can still use the
`FOV_X` fallback for experimentation.

## SSH/tmux workflow

On the GPU PC:

```bash
tmux new -s namma
cd /wherever/Namma
./run.sh pipeline library.mp4
```

Detach: `Ctrl-b`, then `d`. Reattach with `tmux attach -t namma`.

Then download outputs from your laptop with `rsync`, for example:

```bash
rsync -ahP user@gpu-host:/path/to/Namma/outputs/gs_model/library/ ./library_model/
```

## Third-party code

The Docker image clones pinned commits of MASt3R-SLAM, SplatSLAM and Graphdeco Gaussian Splatting. Their licenses/terms remain applicable; credit them in the hackathon report.
