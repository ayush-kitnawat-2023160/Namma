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

## Stage 2: semantic 3D fusion

Stage 2 does **not** reconstruct geometry again. It reuses the confidence-filtered
VGGT dense cloud that NextGen already aligned into the global COLMAP/BA frame:

```text
04_gs_dataset/sparse/0/points3D.ply
             +
03_sfm/model (120 BA cameras)
             +
01_segmentation semantic/instance maps
             |
             v
visibility-aware projection + z-buffer
             |
             +--> multi-view structural voting
             |
             +--> per-frame 3D object fragments
                         |
                         v
                 overlap graph fusion
                         |
                         v
                 persistent 3D objects
```

Persistent objects are associated with **frame-aware one-to-one tracking**.
Fragments are matched only to same-class tracks with sufficient shared 3D point
support and spatial consistency, and a track can never absorb two detections
from the same frame. This avoids the transitive connected-component failure
where a single noisy mask could bridge two physically different chairs/tables.
Tracks are also limited by a configurable frame gap, and exported object points
prefer support from at least two object-mask observations.

Run it after a complete Stage-1 pass:

```bash
./run_semantic.sh semantic-cloud advio_nextgen
```

If tuning requires a rerun:

```bash
./run_semantic.sh semantic-cloud advio_nextgen --overwrite
```

The stage writes:

```text
02_semantic_cloud/
  semantic_scene.ply       semantic palette + semantic metadata
  semantic_scene_rgb.ply   original RGB + semantic metadata
  classes/                 one RGB PLY per semantic class
  instances/               persistent chair/table/sofa/... PLYs
  fragments.json           frame-local 3D fragment diagnostics
  instance_tracks.json     fragment -> persistent-object associations
  labels.json
  summary.json
```

`semantic_scene*.ply` retain `semantic_id`, semantic confidence, vote count,
visibility count and the source dense-point index as PLY vertex properties.
This lets later layout/meshing stages remain traceable back to the original
aligned VGGT geometry.

## Stage 3: explicit room layout

Stage 3 converts the structural class clouds into clean simulator-friendly
geometry without inventing unsupported surfaces:

```text
floor.ply   -> dominant RANSAC plane -> convex floor polygon
wall.ply    -> iterative vertical RANSAC planes -> regularized wall panels
ceiling.ply -> parallel plane only when enough observed support exists
pillar.ply  -> DBSCAN clusters -> oriented box proxies
```

The floor normal is oriented toward the median BA camera position and becomes
the scene `up_vector`. Wall candidates are retained only when their normals are
approximately perpendicular to that up direction. A tiny fraction of extreme
coplanar support is clipped before polygonization so isolated semantic outliers
cannot explode a room boundary.

The quality-v2 path additionally simplifies the observed floor hull and
regularizes each accepted wall into a clean plane-aligned rectangle bounded by
robust quantiles of that wall's actual inlier support. This removes saw-tooth
wall/floor borders without extrapolating the room beyond the observed support
extent.

Run:

```bash
./run_semantic.sh layout advio_nextgen
```

Rerun after tuning with:

```bash
./run_semantic.sh layout advio_nextgen --overwrite
```

Outputs:

```text
03_layout/
  meshes/
    floor.ply
    wall_001.ply
    wall_002.ply
    ...
    ceiling.ply        # only when actually observed
    pillar_001.ply
    ...
  support/
    floor_inliers.ply
    wall_001_inliers.ply
    ...
  room_shell.glb
  layout.json
  summary.json
```

`room_shell.glb` is the first explicit scene asset suitable for loading into a
conventional viewer/engine. At this stage it contains only observed/fitted
architecture; unseen ceilings or walls are deliberately not synthesized.

## Stage 4: observed object meshes

Stage 4 reconstructs each persistent object from the **masked VGGT depth
observations that actually saw it**. It does not run Poisson completion and does
not close unseen backsides.

```text
Stage-2 persistent object track
        |
        +--> contributing frame/local instance IDs
        |       |
        |       +--> Stage-1 SAM2 masks
        |
        +--> decoded VGGT inverse depth
        +--> VGGT depth confidence
        +--> global-BA camera poses
        |
        v
confidence-filtered masked RGB-D observations
        |
        +--> eroded uncertain mask boundary
        +--> back-projected 3D bbox consistency
        |
        v
bounded per-object Uniform TSDF
        |
        v
observed zero-crossing mesh
        |
        +--> reject abnormally long bridge triangles
        +--> small-component removal
        +--> light Taubin smoothing
        v
clean observed mesh
```

The stored depth PNGs are decoded with `inverse_depth_encode_scale` from the
VGGT summary and multiplied by the VGGT->COLMAP Sim(3) scale before they are
integrated with the BA cameras. Object volumes are bounded from the Stage-2 3D
track boxes and adapt their resolution up to a configured maximum, which keeps
memory bounded while preserving detail.

Before TSDF integration, masked depth pixels are now back-projected into the BA
world and rejected when they fall outside an expanded Stage-2 object bounding
box. This is specifically intended to suppress background depth leaking through
SAM boundaries. Long marching-cubes bridge triangles are also removed relative
to the effective voxel size before a very light smoothing pass.

Before meshing, only very strong duplicate Stage-2 tracks are consolidated.
Tracks with any shared source frame are a hard cannot-link, and non-overlapping
tracks need high shared 3D support plus centroid consistency to merge. This is
deliberately conservative: leaving a duplicate is preferable to combining two
physical chairs.

Run:

```bash
./run_semantic.sh object-mesh advio_nextgen
```

Rerun after tuning:

```bash
./run_semantic.sh object-mesh advio_nextgen --overwrite
```

Outputs:

```text
04_object_meshes/
  objects/
    chair_001/
      observed_mesh.ply
      observed_mesh.glb
      metadata.json
    table_001/
      ...
  objects_preview.glb
  objects.json
  summary.json
```

Every object metadata record explicitly states that its geometry comes from
observed masked VGGT depth fused with BA poses and that unseen-surface
completion is `none`.

## Stage 5: UV texture baking

Stage 5 turns the explicit Stage-3/4 geometry into conventional textured assets.
It performs a real UV unwrap with `xatlas`; it does not merely keep the TSDF
vertex colors.

```text
clean architecture meshes + observed object meshes
                 |
                 v
             xatlas UV unwrap
                 |
                 v
       candidate BA source views
                 |
        +--------+---------+
        |                  |
        v                  v
 semantic/instance      VGGT depth
       mask            consistency
        |                  |
        +--------+---------+
                 |
                 v
   best observed RGB view per face
                 |
                 v
      barycentric atlas rasterization
                 |
                 v
           seam padding
                 |
                 v
          textured GLB mesh
```

For object meshes, candidate views come only from the Stage-4 object
observations and the corresponding Stage-1 instance masks. For floor/wall/
pillar/ceiling meshes, the Stage-1 structural semantic masks are used instead.
Every face also has to agree with the decoded VGGT depth in the selected BA
camera before source RGB is accepted. Faces that do not have a trustworthy
source view retain the Stage-4 vertex color when available, or a neutral
fallback for architecture.

Architecture now uses a small coherent source-view set (three by default)
before falling back to per-face view selection. This greatly reduces the
checkerboard/mosaic appearance caused when neighboring wall triangles choose
different exposures from many video frames.

Default atlas sizes are 1024x1024 per object and 2048x2048 per architecture
piece. These can be reduced for a quick test without changing code.

Build the semantic image once after pulling this stage because it adds the
precompiled `xatlas` Python wheel:

```bash
./build_semantic.sh
./run_semantic.sh check
```

Run:

```bash
./run_semantic.sh texture advio_nextgen
```

For a lower-resolution validation run:

```bash
NS_TEXTURE_RESOLUTION=512 \
NS_ARCH_TEXTURE_RESOLUTION=1024 \
./run_semantic.sh texture advio_nextgen --overwrite
```

Outputs:

```text
05_textures/
  architecture/
    floor/
      texture.png
      textured_mesh.glb
      metadata.json
    wall_001/
      ...
  objects/
    chair_001/
      texture.png
      textured_mesh.glb
      metadata.json
    ...
  textured_scene_preview.glb
  assets.json
  summary.json
```

The summary reports the fraction of mesh faces that received trusted source
RGB. This is an important quality-control number: a low value means we should
tune view/depth tolerances before assembling the final scene, rather than
silently painting unsupported geometry.

## Stage 6: final digital-twin scene

Stage 6 assembles the fitted architecture and textured object assets into one
explicit digital-twin package. All visual meshes remain in the same global BA
coordinate frame, so no per-object alignment is needed here.

```text
05_textures textured architecture + objects
                 |
                 +----> visual scene ----------------> digital_twin.glb
                 |
03_layout fitted architecture
                 +----> static collision surfaces ---+
                 |                                    |
04_object_meshes observed objects                     |
                 +----> convex collision proxies -----+--> collision_scene.glb
                 |
03_layout metadata + Stage-4/5 provenance
                 |
                 +----> scene_graph.json
                 |
NextGen BA cameras
                 +----> camera_trajectory.json
```

Architecture collision uses the clean Stage-3 fitted floor/wall/pillar meshes.
Object collision uses conservative convex hulls of the observed Stage-4 object
geometry. If a convex hull is degenerate, the exporter falls back to an
oriented box. This is intended for stable static first-person collision; it is
not yet articulated manipulation-grade collision decomposition.

Run:

```bash
./run_semantic.sh scene advio_nextgen
```

Rerun:

```bash
./run_semantic.sh scene advio_nextgen --overwrite
```

Outputs:

```text
06_scene/
  digital_twin.glb
  collision_scene.glb
  collisions/
    floor.glb
    wall_001.glb
    pillar_001.glb
    chair_001.glb
    ...
  camera_trajectory.json
  scene_graph.json
  summary.json
```

`scene_graph.json` contains one node per semantic asset with its class, visual
mesh, collision mesh, bounds, texture coverage, provenance, source tracks/views
when applicable, and an identity transform because geometry is already stored
in global coordinates.

### Metric scale is intentionally explicit

The monocular reconstruction does **not** guarantee metres. By default Stage 6
therefore writes:

```json
{
  "kind": "reconstruction_units",
  "meters_per_unit": null,
  "metric_calibrated": false
}
```

When one real-world distance is known, compute

```text
meters_per_unit = real_distance_metres / reconstructed_distance_units
```

and rerun only Stage 6:

```bash
NS_SCENE_METERS_PER_UNIT=<computed_real_scale> \
./run_semantic.sh scene advio_nextgen --overwrite
```

Stage 6 then applies the uniform scale to the visual geometry, collision
geometry, and camera trajectory together and records that metric calibration in
the scene graph. Do not copy a made-up example scalar: it must come from an
actual measured distance in the reconstructed scene.

USD export is deliberately deferred. GLB is the validated primary asset for
the current viewer/simulation MVP; a later engine-specific exporter can consume
the same scene graph without changing the reconstruction stages.

## Stage 7: first-person browser walkthrough

Stage 7 packages the Stage-6 visual/collision scenes into a browser runtime.
The viewer uses the fitted Stage-3 up vector to rotate the arbitrary COLMAP
world onto browser `+Y` at runtime; no reconstruction files are rewritten.

```text
06_scene/digital_twin.glb
06_scene/collision_scene.glb
06_scene/scene_graph.json
06_scene/camera_trajectory.json
                 |
                 v
      Babylon.js first-person viewer
                 |
      +----------+-----------+
      |          |           |
      v          v           v
     WASD     mouse look   collision/gravity
      |          |           |
      +----------+-----------+
                 v
          walk through scene
```

Build the package:

```bash
./run_semantic.sh viewer advio_nextgen
```

Outputs:

```text
07_viewer/
  assets/
    digital_twin.glb
    collision_scene.glb
  index.html
  app.js
  style.css
  viewer_config.json
  scene_graph.json
  camera_trajectory.json
  serve.py
  README.txt
  summary.json
```

Serve it with standard Python:

```bash
cd outputs/semantic/advio_nextgen/07_viewer
python3 serve.py --port 8000
```

Then open `http://127.0.0.1:8000` in a browser. When the viewer lives on a
remote GPU server, either copy `07_viewer/` to the local machine or tunnel the
port over SSH. The generated `README.txt` includes the exact pattern.

Controls are mouse-look, WASD movement, Shift sprint, `R` reset to the selected
BA camera pose, `G` gravity toggle, `C` collision debug display, and `H` HUD.
Movement, gravity and the player collider scale from scene bounds so the viewer
still works before metric calibration. If Stage 6 is marked metric but the
scene dimensions look implausibly small, the viewer displays a warning instead
of silently trusting the units.

The runtime currently fetches Babylon.js and its glTF loader from the official
Babylon CDN. The generated geometry/assets themselves are local.

## Implementation order

1. 2D segmentation: structural semantic masks + frame-local object masks. **Implemented.**
2. Semantic RGB-D fusion in the BA/COLMAP world frame. **Implemented.**
3. Layout fitting: robust floor/wall/ceiling/pillar planes. **Implemented.**
4. Per-object masked TSDF fusion and mesh extraction. **Implemented.**
5. Mesh cleanup + simplified collision meshes. **Implemented.**
6. UV/RGB texture baking from the original selected views. **Implemented.**
7. GLB scene + scene graph + collisions; optional USD later. **Implemented.**
8. First-person browser viewer with collision. **Implemented.**

Only after this observed-geometry path is stable should we add a separate
unseen-surface completion stage.
