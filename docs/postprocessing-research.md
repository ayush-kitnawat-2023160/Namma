# Improving reconstruction output after training

Code and saved artifacts reviewed on 2026-10-25, branch `feature/vggt-omega-surface`.
This is an assessment and proposed experiment plan; no models were modified or trained.

## Findings from this repository

The active pipeline is `nextgen/scripts/pipeline.sh`: calibrated/selected RGB →
VGGT-Omega → LightGlue/COLMAP BA → cross-view surface fusion → Graphdeco 3DGS → renders.
The separate MASt3R, semantic-mesh, and panorama paths should not be confused with
the current Gaussian output.

Already implemented:

- `fuse_geometry.py` rejects low-confidence depth and checks depth agreement in
  neighboring views, then confidence/support-weighted voxel fusion creates the initializer.
- `train_gs.sh` enables depth regularization, antialiasing and exposure learning.
- `render_gs.sh` enables antialiasing and exposure application.
- The browser viewer exposes an opacity cutoff through `?alpha=...`.

Missing from the local pipeline: a dedicated final Gaussian cleanup pass,
contribution-based pruning, a pruning/recovery stage, and held-out image evaluation.
Upstream training can prune during optimization; that is different from a final cleanup pass.

Saved runs:

| Run | Registered views | Fused initializer | Final splats | PLY size |
|---|---:|---:|---:|---:|
| `frames_omega_test` | 100/100 | 758,206 | 1,377,699 at 10k | 341.7 MB |
| `retail_competition_160` | 160/160 | 2,000,000 | 2,756,113 at 30k | 683.5 MB |

The retail run already uses stronger fusion than the shell defaults: eight
neighbors, 3% relative depth tolerance, two supporting other views, stride two.
It retained 87.54% of candidate samples before fusion. Recommending those same
settings as a new fix would miss what this run already does.

I compared retail `07_renders/renders/00063.png` with `07_renders/gt/00063.png`.
The rendered image has substantial smearing around chairs, plants and upper wall
detail. This observation is from one image pair, not a complete scene audit. It
indicates a detail/geometry problem beyond ordinary grain. Pose error, depth
constraints and optimization are hypotheses to test, not established causes.

Both saved `cfg_args` have `eval=False`; the current render script uses
`--skip_test`. Current images therefore do not establish generalization to unseen
views. Full camera registration and positive depth scales also do not prove
accurate poses or accurate dense surfaces.

## First: separate viewer quality from model quality

`nextgen/viewer/src/main.js` does not set `sphericalHarmonicsDegree`. The pinned
GaussianSplats3D 0.4.7 viewer defaults to degree zero; its supported maximum is
degree two. Both saved models were trained with degree three. Test degree two,
and compare against Graphdeco from matching cameras before changing geometry.
This can improve view-dependent appearance, but cannot restore details missing
in the offline renders. Exposure transforms stored outside the PLY are another
possible reason browser and training-camera renders differ.

The viewer defaults to `alpha=5`, approximately 5/255 linear opacity. Direct
analysis of the saved float properties, using sigmoid on stored opacity logits,
gives the following candidate counts below each threshold:

| Linear threshold | Retail | Frames test |
|---|---:|---:|
| 1/255 | 52,451 (1.90%) | 3 (<0.01%) |
| 5/255 | 486,082 (17.64%) | 145,457 (10.56%) |
| 10/255 | 834,008 (30.26%) | 410,457 (29.79%) |
| 20/255 | 1,290,789 (46.83%) | 840,164 (60.98%) |

These are exact float comparisons; browser quantization may affect boundary cases.
They are not counts of confirmed noise. Many weak splats can jointly represent a
real surface. Test `alpha=1`, `5`, `10` at matching cameras; do not blindly raise
the production cutoff. Viewer filtering also does not shrink the downloaded PLY.

Sources: [pinned viewer implementation](https://raw.githubusercontent.com/mkkellogg/GaussianSplats3D/v0.4.7/src/Viewer.js),
[viewer options and SH support](https://github.com/mkkellogg/GaussianSplats3D).

## Recommended post-processing

### 1. Conservative Gaussian cleanup — first implementation

Create a separate `08_postprocess/` stage that reads the trained PLY and preserves
all properties for retained rows: positions, SH, opacity, log scales and rotations.
Never pass the Gaussian PLY through a generic XYZ/RGB point-cloud exporter.

Use combined evidence:

1. Validate finite properties and usable rotations/scales.
2. Flag isolated splats using local, density-adaptive neighborhoods.
3. Flag unusually large splats relative to local surface spacing and projected
   footprint; use anisotropy as additional evidence, not an automatic rejection rule.
4. Check geometric support and free-space contradictions across reliable views.
5. Remove weakly supported candidates only when several signals agree.

The existing aligned surface is `05_gs_dataset/sparse/0/points3D.ply`.
`04_geometry/surface.ply` is in the VGGT frame and requires the saved Sim(3).
Distance to that surface is a useful warning signal, not sufficient grounds for
deletion: it is derived from the same uncertain depth and can miss valid details.

For depth checks, use the correct camera convention and coordinate scale. Raw
VGGT depth belongs to VGGT cameras. A global Sim(3) does not eliminate local
changes introduced by BA; simply attaching those depth maps to refined cameras
can create false contradictions. Reproject aligned surface evidence, propagate
uncertainty, and ignore low-confidence pixels and depth discontinuities.
Occluded and unobserved splats should remain unknown rather than automatically bad.

Do not average or smooth Gaussian centers as if they were a conventional point
cloud. Their positions and covariance jointly encode the rendered appearance.

### 2. Benchmark an existing automatic cleaner

PlayCanvas SplatTransform provides `--filter-floaters`, which tests Gaussian
contribution to solid voxels, plus optional seeded cluster filtering. This is a
practical benchmark against a custom cleaner, not a guaranteed quality improvement.
Its voxel sizes are in scene units; our monocular scene is not calibrated in metres.
Avoid blindly copying its defaults or keeping only one connected component, which
could remove separate furniture and thin structures.

Both filters require WebGPU; the existing CUDA container is not sufficient proof
they will run. Validate the documented NVIDIA/Vulkan Docker setup separately,
pin the tested version, and keep standard Gaussian PLY for the current viewer.

Source: [official CLI reference](https://developer.playcanvas.com/user-manual/splat-transform/cli-reference/).

### 3. Contribution pruning followed by recovery optimization

Once conservative cleanup is measured, rank splats by actual rendered contribution
over multiple cameras rather than opacity alone. Prune a small budget and run a
short recovery optimization, initially testing 1–3k iterations with densification
disabled to avoid immediately regrowing the removed artifacts. That iteration
range is a proposed experiment, not a validated setting for these scenes.

LightGaussian demonstrates significance-based pruning with recovery. Its main
purpose is compression; it does not establish that every low-contribution Gaussian
is a floater. Integrating its recovery step also needs explicit model/optimizer
loading: the current wrapper saves a PLY, not a resumable optimizer checkpoint.

Source: [LightGaussian paper and implementation](https://lightgaussian.github.io/).

## Other methods and where they fit

| Method | Use here | Limitation |
|---|---|---|
| Statistical/radius outlier filtering | Optional initializer cleanup or a candidate signal | Center density alone is unsafe for final splats, foliage and thin furniture |
| Image denoising | Mild bilateral/non-local filtering for final still/video exports if actual grain remains | Cannot fix geometry or the interactive PLY; may worsen existing blur |
| Temporal filtering | Motion-compensated filtering for a fixed exported camera path | Requires depth/motion and disocclusion handling; ordinary frame averaging ghosts |
| Sharpening/super-resolution | Final presentation experiment after geometry cleanup | Cannot reliably recover true lost detail; per-frame enhancement can flicker |
| Better depth/normal constraints | Controlled training experiment for walls and textureless surfaces | Requires retraining; uncertain depth can also hurt quality |
| Better visibility sorting | Investigate when splats pop during camera rotation | Renderer work, not PLY denoising |
| Compression/LOD | Reduce download and rendering cost after cleanup | Smaller assets do not inherently mean cleaner reconstruction |

StopThePop addresses sorting-related popping through a different rasterization
approach; it cannot be added as a simple PLY filter.
[StopThePop](https://r4dl.github.io/StopThePop/).

For later training experiments, StableGS uses cross-view consistency and a
dual-opacity formulation; TIDI-GS proposes a training framework for indoor floater
suppression. Neither should be described as a drop-in post-processing switch.
[StableGS](https://arxiv.org/abs/2503.18458),
[TIDI-GS](https://arxiv.org/abs/2601.09291).

## Evaluation and implementation order

1. Compare viewer SH settings and opacity cutoffs on the existing model; no training needed.
2. Save untouched baseline renders and a fixed walkthrough path. Implement a
   separate, reversible cleanup stage with per-rule removal counts and masks.
3. Compare raw, conservative custom cleanup, and SplatTransform cleanup using
   identical cameras, exposure, antialiasing and resolution. Inspect chairs,
   foliage, glass boundaries and newly revealed regions for holes.
4. Report splat count, file size, render time/FPS and image differences. Existing
   training-image PSNR/SSIM/LPIPS are regression checks only. A proper held-out
   comparison requires a separate training run that withholds those images;
   setting `--eval` only after training does not make them unseen.
5. Try contribution pruning/recovery only after the cheaper comparison is useful.
6. If smearing persists, A/B depth weight/confidence masking and pose/depth
   consistency before spending time on image enhancement. The local dataset
   builder currently copies depth PNGs but does not export the cross-view support
   masks into the training depth loss.

Use `09_quality/` for comparison renders, metrics and reports. Keep the trained
model untouched and record the input PLY hash, filter settings, retained indices
and output path. Selection thresholds must be scene-relative until metric scale
is explicitly calibrated.

Graphdeco documents held-out evaluation and the existing depth/antialiasing/
exposure features: [official repository](https://github.com/graphdeco-inria/gaussian-splatting).

**Recommended next development task:** viewer fidelity controls plus an optional
`postprocess` CLI producing a separate cleaned Gaussian PLY and an A/B report.
Do not begin with aggressive opacity pruning or generic image denoising.
