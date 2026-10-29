# Precision: final-camera stereo reconstruction

This is an experimental, isolated upgrade for **one RTX 3090 24 GB and a 128 GB RAM server**. It has been checked with small CPU tests and source inspection locally. **The CUDA image build and real reconstruction have not been run here.** No measured improvement factor is claimed until the same video is compared on the server.

## What improves accuracy

The previous NextGen path refines VGGT cameras with bundle adjustment, but fuses depths with the original VGGT cameras and then fits one global similarity transform. Local camera corrections cannot be recovered by that transform. Its predicted depth is also limited to approximately 512 pixels even when appearance is trained from 4K images. Automatic GeoCalib calibration was being treated as fixed, measured calibration.

Precision uses:

```text
video -> bounded 160-view selection -> VGGT-Omega pose initialization
      -> SuperPoint / LightGlue -> bundle adjustment
         (estimated focal length refined; measured calibration preserved)
      -> sparse geometry quality gate
      -> CUDA COLMAP stereo at up to 1600 pixels with final cameras
      -> geometric consistency across views -> fused cloud
      +-> observed TSDF surface + simplified static collision mesh
      +-> centered-camera dataset + masked stereo inverse-depth priors
         -> bounded, depth-regularized 3DGS with CPU camera storage
         -> withheld-view renders / PSNR and SSIM
```

Every dense artifact uses the final COLMAP coordinate frame. There is no camera-center-only Sim(3) alignment. Zero/unknown depth is masked during training, rather than attracting Gaussians to invalid depth. Camera principal points are explicitly centered for Graphdeco, and RGB/depth/2D tracks are shifted together. This avoids relying on Graphdeco's centered-camera assumption when the refined camera has an off-center principal point.

Higher-resolution stereo is expected to improve supported geometry, particularly textured surfaces with translational overlap. Blank walls, reflections, motion blur, moving people, rolling shutter, and footage with mostly rotation remain difficult. Conservative stereo may produce fewer floaters **and more visible holes**. That is evidence of missing support, not a reason to pretend the holes were measured. VGGT is retained for initialization; stereo does not silently fall back to unverified predicted geometry.

## Backup and isolation

The exact source tree before this upgrade, including the pre-existing uncommitted PanoGraph and untracked PanSplat source, is preserved in:

```text
backup/pre-precision-20261029T142330Z
backups/pre-precision-20261029T142330Z/working-tree.tar.gz
backups/pre-precision-20261029T142330Z/repository.bundle
backups/pre-precision-20261029T142330Z/manifest.json
```

The backup branch is a complete source snapshot commit (`c8f1908`), not just a pointer to the earlier HEAD. The archive has per-file SHA256 checksums in the manifest; the bundle also preserves Git history. Ignored videos, checkpoints, caches and reconstruction outputs remain in place and were not duplicated. Copy those separately if you need an off-machine data backup. `backups/` is excluded from Git and Docker's build context.

Review the old source without changing your checkout:

```bash
git worktree add ../Namma-before-precision backup/pre-precision-20261029T142330Z
```

Alternatively, `git clone backups/pre-precision-20261029T142330Z/repository.bundle ../Namma-restored` restores a separate repository. Select the backup branch there. Keep the archive and bundle outside this PC too if long-term recovery matters.

The old `run_nextgen.sh` and other reconstruction scripts have not been edited. New output lives in `outputs/precision/<scene>`, never in existing NextGen scenes. Input mounts and a refinement source are read-only. Existing destination scenes/models/meshes are refused. Partial outputs are retained after failure; use a new destination to retry after diagnosis.

## Laptop: review and push source only

No model, CUDA, image build, training, large scene loading or reconstruction should be run on the laptop. The local help is safe:

```bash
./run_precision.sh help
```

The working branch is `feature/precision-reconstruction`. Review and commit only this upgrade so unrelated existing edits stay yours:

```bash
git diff -- README.md .gitignore .dockerignore
git add README.md .gitignore .dockerignore build_precision.sh run_precision.sh precision/
git commit -m "Add guarded final-camera stereo reconstruction pipeline"
git push -u origin feature/precision-reconstruction
git push origin backup/pre-precision-20261029T142330Z
```

Nothing has been pushed automatically. The second push preserves the complete old source on GitHub. Videos, checkpoints, outputs and local backup archives are ignored and will not be uploaded by these commands.

## College server: pull, build and check

Commit or stash any server-side source changes first. Then:

```bash
cd /path/to/Namma
git fetch origin
git switch --track origin/feature/precision-reconstruction
git pull --ff-only
export NAMMA_SERVER=1
export NG_GPU_DEVICE=0
./build_precision.sh
./run_precision.sh check
```

If the branch already exists locally, use `git switch feature/precision-reconstruction` instead of `--track`. The existing local `namma-nextgen:latest` image is required. No new packages are installed on the host. The new `namma-precision:latest` image adds CUDA-enabled COLMAP **3.11.1**, pinned at commit `682ea9ac4020a143047758739259b3ff04dabe8d`, and matching `pycolmap==3.11.1`. HLoc is pinned to compatible commit `3bdf494c852f157db57a1cf2039a6c826d52e702` before its camera-rig API migration; inherited model versions remain those of your existing NextGen image. The gated VGGT-Omega checkpoint stays in `nextgen/checkpoints/`.

The image build also runs a tiny CPU-only camera/database compatibility check. The build uses Docker's classic builder, two compiler jobs, 4 CPUs and a 24 GiB build-container memory/swap ceiling. Classic mode resolves the already-local NextGen base and respects the build memory flags. If your Docker installation no longer supports it, the build must fail; **do not remove limits or retry an unrestricted build**. Use a separately resource-limited BuildKit builder with access to your base image, or a server administrator's constrained build environment. Docker's data-root also needs adequate free disk; the script can check the repository filesystem, not a remote daemon's data-root. Allow substantial image/build storage (at least 40 GiB free is checked at the repository).

`check` imports dependencies and checks the COLMAP CLI; it does not allocate CUDA memory or prove reconstruction quality. Heavy commands check the selected GPU before exposing only that GPU to the container. Do not run an old runner or another GPU job concurrently: the lock coordinates Precision jobs/builds **within this checkout**, not unrelated jobs on the server.

## Recover a sparse-quality-gate failure

If the first run stopped at `Sparse quality gates failed` after writing `03_sfm/model`, keep that scene. A disconnected **stereo** neighbor graph does not by itself prove the **camera pose** graph is disconnected: turns and small-baseline views can connect poses but make poor stereo sources. The corrected gate measures these separately and uses all reliable sparse tracks for pair-angle evidence instead of a global strided sample. Reprojection, observation, parallax, geometric stereo and resource protections remain enabled.

This recovery update changes scripts only. Commit/push it on the laptop, then `git pull --ff-only` on the server; **no image rebuild is required** because scripts are mounted read-only into the existing image. The supplied `precision_build.log` confirmed the original server image build succeeded. A CPU-only diagnostic can inspect the saved model:

```bash
export NAMMA_SERVER=1
export NG_GPU_DEVICE=0
PR_ALLOW_VIEW_PRUNING=1 ./run_precision.sh audit precision_v1
```

It prints a report and saves a timestamped `outputs/precision/precision_v1/09_audit/*/quality_report.json`; it does not run feature extraction, BA or stereo. An audit completes even when `passed` is false, so read that field and the failures. With `PR_ALLOW_VIEW_PRUNING=1` it proposes a retained subset; without it all views must pass.

Recover into a new scene while preserving `precision_v1` and skipping VGGT/feature matching/BA:

```bash
set -o pipefail
PR_ALLOW_VIEW_PRUNING=1 PR_TRAIN=0 \
  ./run_precision.sh recover precision_v1 precision_v2 \
  2>&1 | tee outputs/logs/precision_v2_geometry.log
```

The command copies saved frames and the final SfM model (reflinks where available); it does not copy or overwrite the failed dense stage. Explicit pruning drops cameras with inadequate observations/stereo sources, requires every retained reference to have at least two retained sources, and requires **at least 80% of the original views**. It retains one connected camera-overlap component, removes excluded camera observations from an in-memory model copy, and checks the resulting model again before stereo. The original cameras and files remain untouched. Pose or stereo quality is not guaranteed merely by passing these checks.

Read `precision_v2/04_mvs/quality_report.json` for every exclusion and `retained_quality_report.json` for validation after filtering. A high retained frame fraction does not guarantee coverage of every part of a room: inspect what was excluded before training. Unsupported surfaces are not filled in. If too many views would be lost or the retained model fails QA, recovery stops and retains its reports. Send those JSON reports for diagnosis rather than reducing all thresholds.

After successful geometry inspection:

```bash
./run_precision.sh report precision_v2
./run_precision.sh train precision_v2
```

## Best first experiment: reuse your existing video inference

Run inside `tmux` on the server. Substitute your actual existing NextGen scene name:

```bash
export NAMMA_SERVER=1
export NG_GPU_DEVICE=0
PR_TRAIN=0 ./run_precision.sh refine retail_competition_160 retail_precision_v1
./run_precision.sh report retail_precision_v1
```

This reuses/copies `00_calibration`, `01_frames` and `02_vggt` from `outputs/nextgen/retail_competition_160`, reruns corrected SfM/BA, and creates stereo geometry, a GS dataset and observed/collision meshes. VGGT is not rerun. Copies use reflinks when the filesystem supports them; otherwise allow space for the selected frames and depth files. Old trained models and outputs remain untouched.

Inspect these results before spending time training:

```text
outputs/precision/retail_precision_v1/
  03_sfm/                    corrected final cameras and tracks
  04_mvs/quality_report.json  sparse evidence / gate failures
  04_mvs/dense/fused.ply      final-camera stereo point cloud
  05_gs_dataset/summary.json  per-view supported depth coverage
  08_mesh/observed_surface.ply
  08_mesh/collision_static.ply
  08_mesh/summary.json
  precision_report.json
```

If camera geometry or overlap is insufficient the quality gate stops the run. Inspect `04_mvs/quality_report.json`, source imagery and calibration; recapture missing overlap rather than weakening every threshold. High reprojection error often indicates camera/calibration problems; low triangulation angles indicate insufficient camera translation. These gates detect some failures, not all wrong reconstructions.

Train only after inspecting geometry:

```bash
./run_precision.sh train retail_precision_v1
./run_precision.sh report retail_precision_v1
```

Outputs include `06_gs_model/point_cloud/iteration_30000/point_cloud.ply`, withheld RGB renders under `06_gs_model/test/`, and `06_gs_model/results.json`. Every eighth selected frame is held out from Gaussian optimization; all frames still contribute to camera estimation and stereo initialization, and neighboring video frames remain correlated, so these scores are only an appearance check. PSNR/SSIM evaluation streams one pair on CPU and downloads no metric-network weights. A quantitative 3D accuracy claim requires measured distances or an independent reference scan. Compare wall alignment, duplicated surfaces, floaters, holes and visible detail at the same viewpoints against the old reconstruction. Do not compare training-view scores against held-out scores as if they were the same evaluation.

## A new video

Place the file under `data/` and run on the server:

```bash
NAMMA_SERVER=1 NG_GPU_DEVICE=0 NG_TARGET_VIEWS=160 \
  ./run_precision.sh pipeline library.mp4 library_precision_v1
```

For a genuinely calibrated camera, add `NG_CAMERA_PROFILE=<profile-basename>`. Do not use the ADVIO profile for an unrelated phone. Automatic GeoCalib estimates initialize the reconstruction and allow focal BA; measured profiles keep focal calibration fixed. Automatic distortion is still an estimate: this version does not jointly re-estimate lens distortion and re-undistort after BA. If straight lines remain curved or double, use a measured matching calibration and recapture without changing zoom/focus.

Optional conservative settings:

| Setting | Default | Purpose |
| --- | --- | --- |
| `PR_TRAIN=0` | `1` | Inspect geometry before training |
| `NG_TARGET_VIEWS` | `160` (max 160) | Lower to 100–120 if VGGT's planner refuses |
| `PR_MVS_MAX_IMAGE_SIZE` | `1600` | Bounded stereo resolution; raise only after baseline validation |
| `PR_MVS_NEIGHBORS` | `12` | Bounded covisible source views |
| `PR_INITIAL_POINTS` | `400000` | Maximum GS initializer points |
| `PR_MAX_GAUSSIANS` | `1200000` (max 1.5M) | Limits densification, including temporary split growth |
| `NG_GS_ITERS` | `30000` | First quicker experiment: `7000` |
| `NG_GS_RESOLUTION` | `1` | Dataset already bounded at stereo resolution; `2` halves it |
| `NG_GPU_MEMORY_FRACTION` | `0.85` (max `0.88`) | Torch allocator cap; custom CUDA allocations are separate |
| `PR_TSDF_VOXEL_DIVISOR` | `512` | Median depth / divisor; scale-relative mesh resolution |

## Resource protection

Heavy commands require explicit `NAMMA_SERVER=1`, at least **100 GiB physical and 92 GiB currently available RAM**, a selected GPU with **22 GiB total / 18 GiB free VRAM**, and working Docker memory/swap controls. Decimal “128 GB RAM” machines typically provide about 119 GiB and can pass while idle. These requirements intentionally prevent a laptop from starting a job and refuse an already busy server.

The container has an **80 GiB RAM ceiling, no additional swap allowance, 8 CPU quota, 4 numerical-library threads, private IPC, 4 GiB shared memory and 1,024 PID limit**. Cache sizes and stereo neighbors are bounded. Training retains camera RGB/depth on CPU and copies sampled tensors to CUDA. Densification is capped and an allocator limit is applied to model inference/training. COLMAP's custom CUDA stereo memory is controlled through image size and neighbor count; Torch's allocator cap does not constrain it.

The watchdog polls at most every five seconds and terminates the process tree at GPU temperature >=83°C, free VRAM <1 GiB, available host RAM <12 GiB, free output disk <15 GiB, or failed telemetry. Safety stop returns **75**. Log records live in `outputs/logs/precision_watchdog_*.jsonl`. Lower temperature limits / larger reserves may be set with `PR_*` watchdog variables; they cannot relax the profile's minimum protections.

An allocation burst may fail before the watchdog sees it. Container OOM and driver/CUDA failure can still terminate a job; no software guard guarantees a machine cannot crash. The pipeline never increases limits or launches a second model automatically after failure. Partial outputs and scheduled training checkpoints are retained for diagnosis. The `train` command does not automatically resume a partial Graphdeco model.

## Physics and incomplete coverage

The output collision mesh is for a **static triangle collider**. It makes walking, raycasts and collision possible in a game/physics engine after choosing the correct up direction and scale. It does not improve the camera reconstruction itself. The existing semantic pipeline contains object/layout concepts, but its old hardcoded NextGen inputs are **not automatically compatible** with Precision; this upgrade does not claim a new interactive physics simulator.

Monocular reconstruction has arbitrary scale. Measure one known distance and set `meters_per_unit = real_metres / reconstructed_units` when importing visual and collision geometry together into your engine. Do not turn arbitrary reconstruction units into physical mass, gravity or speed without that calibration.

No method can verify the back of a chair or a room corner absent from the video. Generative completion could make a visually plausible separate asset but must remain labeled as guessed geometry. The most effective additional input is a second slow overlapping pass with camera translation: walk around objects, observe each surface from several angles, revisit the starting area for loop closure, avoid whip pans, moving people and changing zoom, and include a measured scale reference. More VRAM and physics cannot substitute for those missing observations.

## Lightweight validation

On a development machine, these checks do not run models, Docker builds or reconstruction:

```bash
bash -n build_precision.sh run_precision.sh precision/scripts/entrypoint.sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s precision/tests -v
```

Tests use small synthetic arrays, mock telemetry/CLI calls and tiny subprocesses. No dependency installation is required for the stdlib safety tests; numerical tests use already-available NumPy/OpenCV. Temporary test directories are removed automatically. The backup artifacts are intentionally retained.

Local validation for this change: **41 tests passed**, Python source parsed, shell syntax passed, and the backup archive/branch were independently checked against all 117 original source-file hashes. Apart from the README and ignore-file additions, original source files match their pre-upgrade hashes. This does not establish that the CUDA image builds or that a real scene passes the quality gates; those checks must run on the college server.

## Primary references

- [COLMAP: reconstructing dense geometry from known/refined poses](https://colmap.github.io/faq.html#reconstruct-sparse-dense-model-from-known-camera-poses)
- [Pinned COLMAP stereo options/source](https://github.com/colmap/colmap/tree/682ea9ac4020a143047758739259b3ff04dabe8d/src/colmap/mvs)
- [HLoc triangulation implementation used by NextGen](https://github.com/cvg/Hierarchical-Localization/blob/c13273bd0ecc2917a35910fd843712a1c6243193/hloc/triangulation.py)
- [Graphdeco: memory guidance, depth regularization and evaluation](https://github.com/graphdeco-inria/gaussian-splatting)
- [Docker classic builder and resource flags](https://docs.docker.com/reference/cli/docker/image/build/)
