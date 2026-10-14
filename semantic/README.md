# Namma Semantic Digital-Twin Extension

This branch extends the proven NextGen reconstruction **without modifying it**.

The existing `feature/nextgen-reconstruction` branch remains the reproducible
RGB -> VGGT-Omega -> LightGlue/COLMAP BA -> depth-regularized 3DGS baseline.
This branch adds a second, isolated pipeline that consumes those artifacts
read-only and builds an explicit semantic/mesh world for first-person use.

## Isolation / rollback model

```text
feature/nextgen-reconstruction             feature/semantic-digital-twin
--------------------------------           ------------------------------
run_nextgen.sh                             run_semantic.sh
nextgen/                                   semantic/
namma-nextgen:latest                       namma-semantic:latest
outputs/nextgen/<scene>/       --RO-->     outputs/semantic/<scene>/
```

`run_semantic.sh` mounts `outputs/nextgen` as **read-only** inside its
container, so a semantic experiment cannot overwrite the known-good camera,
depth, SfM or Gaussian artifacts.

Switch back to the proven reconstruction at any time:

```bash
git switch feature/nextgen-reconstruction
```

Return to the semantic extension:

```bash
git switch feature/semantic-digital-twin
```

No NextGen outputs need to be regenerated when switching branches.

## Architecture

```text
                                  RGB VIDEO
                                      |
                                      v
                         EXISTING NEXTGEN PIPELINE
                                      |
             +------------------------+------------------------+
             |                        |                        |
             v                        v                        v
       selected RGB              VGGT-Omega                COLMAP BA
       full resolution        depth/confidence          refined cameras
             |                        |                        |
             +------------------------+------------------------+
                                      |
                       READ-ONLY SEMANTIC INPUT CONTRACT
                                      |
                                      v
                         +-------------------------+
                         | 2D SCENE UNDERSTANDING  |
                         |                         |
                         | stuff: floor/wall/etc. |
                         | things: chair/table... |
                         | per-instance masks     |
                         +------------+------------+
                                      |
                        RGB + mask + depth + pose
                                      |
                                      v
                         +-------------------------+
                         | SEMANTIC RGB-D FUSION  |
                         |                         |
                         | XYZ RGB class instance|
                         | confidence             |
                         +------------+------------+
                                      |
                   +------------------+------------------+
                   |                                     |
                   v                                     v
          +-------------------+                 +-------------------+
          | ARCHITECTURAL     |                 | OBJECT INSTANCES  |
          | GEOMETRY          |                 |                   |
          |                   |                 | chair_001         |
          | floor / walls     |                 | table_001         |
          | ceiling / pillar  |                 | sofa_001          |
          +---------+---------+                 +---------+---------+
                    |                                     |
                    v                                     v
             RANSAC planes                         masked RGB-D/TSDF
             polygonization                              fusion
                    |                                     |
                    v                                     v
             CLEAN ROOM SHELL                    INDIVIDUAL MESHES
                    |                                     |
                    +------------------+------------------+
                                       |
                                       v
                               TEXTURE BAKING
                           from original RGB views
                                       |
                                       v
                                SCENE GRAPH
                        semantics + transforms +
                       visual mesh + collision mesh
                                       |
                                +------+------+
                                |             |
                                v             v
                               GLB           USD
                                |             |
                                +------+------+
                                       |
                                       v
                              FIRST-PERSON WORLD
                         WASD + mouse + collision

       Existing 3DGS remains available in parallel as a visual reference
       or later hybrid renderer. It is not overwritten by this pipeline.
```

## Stage layout

```text
outputs/semantic/<scene>/
  00_source/          immutable manifest of NextGen inputs
  01_segmentation/    semantic + instance masks
  02_semantic_cloud/  fused 3D labels / instance point clouds
  03_layout/          floor/wall/ceiling/pillar planes + room shell
  04_object_meshes/   one observed-surface mesh per object
  05_textures/        baked texture/vertex-color assets
  06_scene/           scene.glb, optional scene.usd, scene_graph.json
  07_viewer/          first-person runtime package
```

## Why this solves a different problem than 3DGS

The Gaussian reconstruction remains the best photorealistic representation of
surfaces actually observed by the input video. The semantic extension creates
explicit geometry and object identity so the environment can provide collision,
object selection, physics and conventional rendering from viewpoints that are
not tied to the captured camera path.

This branch deliberately does **not** solve unseen/back-side completion yet.
Generated meshes represent only supported/observed geometry plus robust fitted
layout surfaces.

## Build and verify the extension

Build a separate image:

```bash
./build_semantic.sh
```

Check dependencies:

```bash
./run_semantic.sh check
```

Validate a completed NextGen scene without changing it:

```bash
./run_semantic.sh source advio_nextgen
```

This writes only:

```text
outputs/semantic/advio_nextgen/00_source/manifest.json
```

Check stage status:

```bash
./run_semantic.sh status advio_nextgen
```

## Stage 1: 2D scene understanding

Stage 1 is now implemented as a hybrid segmentation stack:

```text
selected full-resolution RGB
        |
        +--> Mask2Former / ADE20K
        |      -> floor / wall / ceiling / pillar / door semantic map
        |
        +--> Grounding DINO
               -> chair / table / sofa / cabinet / door boxes
                        |
                        v
                       SAM2
                        -> precise per-object masks
```

The models are intentionally loaded through Transformers rather than installing
the custom Grounding-DINO/SAM2 CUDA repositories into the validated NextGen
environment. Hugging Face model files are cached persistently under
`semantic/hf_cache/` and are not committed to git.

For the first test, run only a few evenly spaced frames:

```bash
./run_semantic.sh segment advio_nextgen --limit 8
```

Inspect:

```text
outputs/semantic/advio_nextgen/01_segmentation/contact_sheet.jpg
```

If those masks look correct, replace the preview with all 120 views:

```bash
./run_semantic.sh segment advio_nextgen --overwrite
```

Stage 1 writes:

```text
01_segmentation/
  semantic/       uint8 structural class maps (0=background)
  masks/          individual binary object masks per frame
  instance_maps/  uint16 frame-local instance-ID maps
  instances/      per-frame boxes, scores, classes and mask paths
  overlays/       RGB quality-control overlays
  labels.json
  detections.json
  summary.json
  contact_sheet.jpg
```

Instance IDs in this stage are **frame-local**. Stage 2 will fuse repeated 2D
observations through the BA coordinate frame and assign persistent 3D object IDs.

Useful threshold overrides are available without editing code, for example:

```bash
NS_BOX_THRESHOLD=0.25 \
NS_MIN_MASK_AREA=150 \
./run_semantic.sh segment advio_nextgen --overwrite --limit 12
```

The default configuration also applies a stricter score threshold to `door`
detections than to chairs/tables/sofas. This keeps the global detector threshold
low enough for partially occluded furniture while reducing false door boxes on
windows, wall panels and other large rectangular regions.

## Implementation order

1. 2D segmentation: structural semantic masks + frame-local object masks. **Implemented.**
2. Semantic RGB-D fusion in the BA/COLMAP world frame.
3. Layout fitting: robust floor/wall/ceiling/pillar planes.
4. Per-object masked TSDF fusion and mesh extraction.
5. Mesh cleanup + simplified collision meshes.
6. RGB texture/vertex-color baking from the original selected views.
7. GLB scene + scene graph, then optional USD.
8. First-person viewer with collision.

Only after this observed-geometry path is stable should we add a separate
unseen-surface completion stage.
