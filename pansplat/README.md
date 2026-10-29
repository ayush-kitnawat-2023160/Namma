# Namma PanSplat transitions

This stage augments the working PanoGraph pipeline with offline Gaussian novel-view transitions.
It intentionally keeps the original captured panorama at every station and uses PanSplat only between graph-connected stations.

## Design

```
PanoVGGT poses + PanoGraph edge + two panoramas
                    |
                    v
                PanSplat
                    |
                    v
      edge transition .webm (offline)
```

The runtime is isolated from `namma-panograph:latest` because upstream PanSplat currently targets PyTorch 2.4 / CUDA 11.8 while PanoVGGT uses a newer CUDA stack.

## Commands

```bash
./run_pansplat.sh build
./run_pansplat.sh check
PS_PROFILE=512 ./run_pansplat.sh gpu-check
./run_pansplat.sh models 512

# one-edge smoke test first
PS_PROFILE=512 PS_FRAMES=24 ./run_pansplat.sh edge \
  hindoor_unibs station_000 station_001 --profile 512

# then all graph edges, one process per edge
PS_PROFILE=512 ./run_pansplat.sh render hindoor_unibs --profile 512

./run_pansplat.sh validate hindoor_unibs
```

After the 512x1024 path is validated, fetch and test the released 2048x4096 checkpoint:

```bash
./run_pansplat.sh models 4k
PS_PROFILE=4k ./run_pansplat.sh gpu-check
PS_PROFILE=4k PS_FRAMES=36 ./run_pansplat.sh edge \
  hindoor_unibs station_000 station_001 --profile 4k
```

The 4K profile is deliberately edge-by-edge and requires nearly all of an RTX 3090. The wrapper checks free VRAM before starting. A CUDA OOM kills only that edge container/process; it does not automatically increase the memory cap.

Outputs are written to:

```
outputs/panograph/<scene>/05_pansplat/transitions/
```
