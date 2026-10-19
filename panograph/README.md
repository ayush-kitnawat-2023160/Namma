# Namma PanoGraph

Modern RGB-only panoramic tour pipeline for the `feature/panograph-modern` branch.

The default experiment uses only the ten RGB equirectangular panoramas from the
`unibs` laboratory scene in HIndoor-8K. The LiDAR depth files are deliberately
not downloaded or used as input.

## Pipeline

```text
HIndoor-8K / unibs / RGB only
        |
        v
10 real 8192x4096 panoramas
        |
        v
PanoVGGT (2026)
pairwise inference, two panoramas per forward pass
        |
        v
relative station poses -> chained camera trajectory
        |
        v
PCA floor projection + navigation graph
        |
        v
SPHR (Next.js + Three.js) 360 tour
```

The pairwise PanoVGGT design is intentional. Running a 1B-parameter geometry
model over all ten high-resolution panorama views in one forward pass creates an
unnecessary OOM risk on a shared RTX 3090. Namma loads the model once, then runs
two panoramas at a time and chains their relative camera transforms.

If geometry inference fails, the full `pipeline` command still produces a
working ten-node panorama tour using a deterministic fallback graph. That keeps
viewer development independent from GPU availability while making the failure
explicit in the logs/status.

## Host requirements

Only these are expected on the host:

- Git
- Docker
- NVIDIA Container Toolkit for GPU commands

Do **not** install Python, PyTorch, Conda, Node, PanoVGGT or SPHR on the host.
They are all inside `namma-panograph:latest`.

## Laptop -> college-server workflow

On the laptop:

```bash
cd /home/ayush/Desktop/Namma
git switch feature/panograph-modern
git push -u origin feature/panograph-modern
```

On the college server:

```bash
cd /home/dev/Ayush/Namma
git fetch origin
git switch feature/panograph-modern
git pull
```

Build the isolated image:

```bash
./run_panograph.sh build
```

Verify container dependencies without a GPU:

```bash
./run_panograph.sh check
```

Check that the selected GPU is sufficiently free before inference:

```bash
./run_panograph.sh gpu-check
```

Run the complete pipeline:

```bash
NP_OVERWRITE=1 ./run_panograph.sh pipeline hindoor_unibs
```

This command automatically:

1. downloads only `ushah/HIndoor-8K/unibs/rgb/*` from Hugging Face;
2. downloads the official high-resolution PanoVGGT `model.pt` checkpoint;
3. validates the 2:1 panoramas and produces 4096x2048 WebP viewer copies;
4. runs memory-capped pairwise PanoVGGT pose estimation;
5. builds the PanoGraph;
6. creates an SPHR bootstrap package.

The PanoVGGT Docker image also pre-caches its DINOv2 ViT-L/14 backbone during
`./run_panograph.sh build`. This is intentional: upstream PanoVGGT otherwise
downloads the ~1.1 GB DINOv2 backbone through `torch.hub` on first model load.
No model download is allowed to appear unexpectedly in the GPU inference stage.

Check status:

```bash
./run_panograph.sh status hindoor_unibs
./run_panograph.sh validate hindoor_unibs
```

Start the viewer:

```bash
./run_panograph.sh serve hindoor_unibs
```

Then open:

```text
http://127.0.0.1:3000/?config=/datasets/panograph/hindoor_unibs/bootstrap.json
```

For a remote server, tunnel the port from the laptop:

```bash
ssh -L 3000:127.0.0.1:3000 <college-server>
```

## Stage-by-stage commands

```bash
./run_panograph.sh fetch
./run_panograph.sh models
./run_panograph.sh prepare hindoor_unibs
./run_panograph.sh gpu-check
./run_panograph.sh reconstruct hindoor_unibs
./run_panograph.sh graph hindoor_unibs
./run_panograph.sh viewer hindoor_unibs
./run_panograph.sh serve hindoor_unibs
```

## GPU and server-safety policy

The wrapper applies a Docker cgroup RAM limit and disables additional swap use:

```text
--memory=32g
--memory-swap=32g
--pids-limit=2048
--shm-size=16g
```

GPU inference additionally uses two guards:

1. it refuses to start if less than 18,000 MiB VRAM is free;
2. PyTorch is capped to 82% of the visible GPU with
   `torch.cuda.set_per_process_memory_fraction`.

Defaults:

```bash
NP_GPU_DEVICE=0
NP_GPU_MIN_FREE_MB=18000
NP_GPU_MEMORY_FRACTION=0.82
NP_RAM_LIMIT=32g
NP_SHM_SIZE=16g
```

An allocation above the PyTorch cap should fail the inference process/container
rather than intentionally consuming all GPU memory. CUDA drivers and Docker do
not provide a perfect VRAM cgroup equivalent, so no software wrapper can promise
that a GPU driver can never fail. The design minimizes that risk by using
two-view forwards, a preflight free-memory gate, and a conservative PyTorch cap.

Do not raise `NP_GPU_MEMORY_FRACTION` on a shared GPU just to force a run.

## Outputs

```text
outputs/panograph/hindoor_unibs/
  01_panoramas/
    manifest.json
    source/                 normalized original copies
    web/                    4096x2048 WebP copies
  02_geometry/
    station_c2w.npy
    poses.json
    summary.json
  03_graph/
    graph.json
    summary.json
  04_sphr/
    bootstrap.json
    graph.json
    summary.json
    assets/panos/
```

Persistent caches/downloads are kept outside Git but inside bind-mounted Docker
workspaces:

```text
data/panograph/
panograph/checkpoints/
panograph/hf_cache/
panograph/npm_cache/
```

## Why PanSplat is not in the mandatory path yet

PanSplat (CVPR 2025) remains the intended learned transition upgrade, but its
official pretrained checkpoints are currently distributed through SharePoint
links rather than a stable programmatic model registry. The official 4K model
also states a minimum 24 GB GPU requirement, leaving essentially no safety
margin on an RTX 3090.

Therefore the reproducible branch first establishes:

```text
RGB panoramas -> PanoVGGT geometry -> graph -> SPHR tour
```

SPHR already provides panorama-to-panorama camera/crossfade navigation. PanSplat
should be added as an **optional offline transition renderer** after this baseline
is validated, using its 512x1024 checkpoint by default. It must not be allowed to
make the base tour dependent on a fragile checkpoint host or a near-100%-VRAM
4K run.

## Upstream versions pinned in the image

- PanoVGGT: `556bb7d2ec2d02bd3ee4ed535542e74290ba22cf`
- SPHR: `9ba9424face778da660ea5e0283af162d1698b4f`

The image builds these repositories at fixed commits so laptop and server runs
do not silently change when upstream `main` changes.
