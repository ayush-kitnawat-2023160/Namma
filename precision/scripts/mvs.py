#!/usr/bin/env python3
"""Bounded COLMAP 3.11.1 stereo in the refined SfM coordinate system.

Never rewrites the source scene. Partial products and logs survive failures.
CLI option names are checked against the upstream 3.11.1 option_manager.cc.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import subprocess
import time
from pathlib import Path

import numpy as np


def read_colmap_array(path: Path) -> np.ndarray:
    """Read a COLMAP float32 depth/normal map without loading the full file.

    Preserves H/W even when either dimension is one. Rejects malformed headers,
    truncated data and trailing bytes before mapping anything into memory.
    """
    path = Path(path)
    fields, token = [], bytearray()
    with path.open("rb") as handle:
        for _ in range(96):
            value = handle.read(1)
            if value == b"&":
                if not token or not token.isdigit():
                    raise ValueError(f"Invalid COLMAP array header: {path}")
                fields.append(int(token))
                token.clear()
                if len(fields) == 3:
                    break
            elif value and value.isdigit():
                token.extend(value)
            else:
                raise ValueError(f"Invalid COLMAP array header: {path}")
        if len(fields) != 3:
            raise ValueError(f"Unterminated COLMAP array header: {path}")
        offset = handle.tell()
    width, height, channels = fields
    if not (0 < width <= 32768 and 0 < height <= 32768 and channels in (1, 3)):
        raise ValueError(f"Unsafe COLMAP array dimensions {fields}: {path}")
    count = width * height * channels
    if path.stat().st_size != offset + count * 4:
        raise ValueError(f"COLMAP array payload size mismatch: {path}")
    payload = np.memmap(path, mode="r", dtype="<f4", offset=offset, shape=(count,))
    result = payload.reshape((width, height, channels), order="F").transpose(1, 0, 2)
    return result[:, :, 0] if channels == 1 else result


def camera_center(image) -> np.ndarray:
    value = image.projection_center
    return np.asarray(value() if callable(value) else value, dtype=np.float64).reshape(3)


def ply_vertex_count(path: Path) -> int:
    """Inspect only the bounded header; do not load a potentially huge cloud."""
    with Path(path).open("rb") as handle:
        if handle.readline() != b"ply\n":
            raise ValueError(f"Invalid fused PLY: {path}")
        count, consumed = None, 4
        for _ in range(256):
            line = handle.readline(1024)
            consumed += len(line)
            if consumed > 65536 or not line:
                break
            if line.startswith(b"element vertex "):
                count = int(line.split()[2])
            if line.strip() == b"end_header" and count is not None and count >= 0:
                return count
    raise ValueError(f"Missing/unsafe fused PLY header: {path}")


def quality_report(reconstruction, *, neighbors=12, min_points=500,
                   min_observations=50, min_shared_points=30,
                   max_median_error=2.0, max_p95_error=4.0,
                   min_median_angle=1.5, min_pair_angle=1.0) -> dict:
    """Fail-closed sparse QA. Registration alone is not evidence of geometry.

    Uses actual finite triangulated tracks, stored post-BA point reprojection
    errors, per-camera observations, parallax and connected covisibility.
    Angular diagnostics use at most 5,000 deterministically sampled tracks.
    """
    images = dict(reconstruction.images.items())
    image_ids = sorted(images)
    index = {image_id: i for i, image_id in enumerate(image_ids)}
    n = len(image_ids)
    centers = np.asarray([camera_center(images[k]) for k in image_ids])
    problems = []
    if n < 3:
        problems.append("At least three cameras are required")
    if centers.shape != (n, 3) or not np.isfinite(centers).all():
        raise ValueError("Camera centers are nonfinite or malformed")
    shared = np.zeros((n, n), dtype=np.int32)
    angle_sums = np.zeros((n, n), dtype=np.float64)
    angle_counts = np.zeros((n, n), dtype=np.int32)
    observations = np.zeros(n, dtype=np.int64)
    errors, lengths, max_angles = [], [], []
    rejected = 0
    sample_stride = max(1, math.ceil(len(reconstruction.points3D) / 5000))
    for point_index, point in enumerate(reconstruction.points3D.values()):
        xyz = np.asarray(point.xyz, dtype=np.float64)
        error = float(point.error)
        ids = sorted({index[e.image_id] for e in point.track.elements if e.image_id in index})
        if len(ids) < 2 or xyz.shape != (3,) or not np.isfinite(xyz).all() or not math.isfinite(error) or error < 0:
            rejected += 1
            continue
        errors.append(error)
        lengths.append(len(ids))
        # Error statistics include bad points; only reliable tracks vote for
        # overlap. Otherwise a corrupt wall could create a convincing graph.
        if error > max_p95_error:
            continue
        observations[ids] += 1
        pairs = list(itertools.combinations(ids, 2))
        if pairs:
            rows, cols = np.asarray(pairs).T
            shared[rows, cols] += 1
            shared[cols, rows] += 1
        if point_index % sample_stride:
            continue
        rays = xyz - centers[ids]
        norms = np.linalg.norm(rays, axis=1)
        if np.any(norms < 1e-10):
            continue
        rays /= norms[:, None]
        angles = np.degrees(np.arccos(np.clip(rays @ rays.T, -1.0, 1.0)))
        max_angles.append(float(angles.max()))
        angle_sums[np.ix_(ids, ids)] += angles
        angle_counts[np.ix_(ids, ids)] += 1

    median_error = float(np.median(errors)) if errors else None
    p95_error = float(np.percentile(errors, 95)) if errors else None
    median_angle = float(np.median(max_angles)) if max_angles else None
    median_track = float(np.median(lengths)) if lengths else None
    if len(errors) < min_points:
        problems.append(f"Only {len(errors)} valid sparse points; need {min_points}")
    if rejected:
        problems.append(f"{rejected} malformed/untracked/nonfinite sparse points")
    if median_error is None or median_error > max_median_error:
        problems.append(f"Median point reprojection error {median_error} exceeds {max_median_error}px")
    if p95_error is None or p95_error > max_p95_error:
        problems.append(f"95th percentile reprojection error {p95_error} exceeds {max_p95_error}px")
    if median_track is None or median_track < 3:
        problems.append("Median sparse track length must be at least three views")
    if median_angle is None or median_angle < min_median_angle:
        problems.append(f"Median maximum triangulation angle {median_angle} is below {min_median_angle} degrees")
    mean_angles = np.divide(angle_sums, angle_counts, out=np.zeros_like(angle_sums), where=angle_counts > 0)
    graph = (shared >= min_shared_points) & (mean_angles >= min_pair_angle)
    np.fill_diagonal(graph, False)
    seen, pending = set(), [0] if n else []
    while pending:
        i = pending.pop()
        if i in seen:
            continue
        seen.add(i)
        pending.extend(int(j) for j in np.flatnonzero(graph[i]) if int(j) not in seen)
    if len(seen) != n:
        problems.append(f"Reliable covisibility graph is disconnected ({len(seen)}/{n} reachable views)")
    per_image, source_neighbors = [], {}
    for i, image_id in enumerate(image_ids):
        name = images[image_id].name
        candidates = [int(j) for j in np.flatnonzero(graph[i])]
        candidates.sort(key=lambda j: (-int(shared[i, j]), -float(mean_angles[i, j]), images[image_ids[j]].name))
        selected = candidates[:neighbors]
        if observations[i] < min_observations:
            problems.append(f"{name}: {observations[i]} reliable observations; need {min_observations}")
        if len(selected) < 2:
            problems.append(f"{name}: fewer than two covisible views with adequate parallax")
        source_neighbors[name] = [images[image_ids[j]].name for j in selected]
        per_image.append({"name": name, "reliable_observations": int(observations[i]),
                          "source_images": source_neighbors[name]})
    return {"passed": not problems, "failures": problems, "registered_images": n,
            "valid_sparse_points": len(errors), "invalid_sparse_points": rejected,
            "median_track_length": median_track, "median_point_error_px": median_error,
            "p95_point_error_px": p95_error, "median_max_triangulation_angle_degrees": median_angle,
            "parallax_sample_tracks": len(max_angles), "connected_views": len(seen),
            "source_neighbors": source_neighbors, "per_image": per_image,
            "thresholds": {"min_points": min_points, "min_observations": min_observations,
                           "min_shared_points": min_shared_points, "max_median_error_px": max_median_error,
                           "max_p95_error_px": max_p95_error, "min_median_angle_degrees": min_median_angle,
                           "min_pair_angle_degrees": min_pair_angle, "max_source_neighbors": neighbors}}


def run_command(output: Path, name: str, command: list[str]) -> None:
    log_path = output / "logs" / f"{name}.log"
    entry = {"stage": name, "argv": command, "started_unix": time.time(), "log": str(log_path)}
    print(f"[precision] {name}; log: {log_path}", flush=True)
    with log_path.open("w") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False)
    entry.update(returncode=result.returncode, elapsed_seconds=time.time() - entry["started_unix"])
    with (output / "command_log.jsonl").open("a") as handle:
        handle.write(json.dumps(entry) + "\n")
    if result.returncode:
        raise RuntimeError(f"{name} failed ({result.returncode}); inspect {log_path}. Partial output retained.")


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def validate_output_location(source: Path, output: Path) -> None:
    """Allow a new 04_mvs in a copied precision scene, never input stages."""
    if output == source or output in source.parents:
        raise ValueError("Output cannot replace the scene or an ancestor")
    if source in output.parents and output.relative_to(source).parts[0] in {
        "00_calibration", "01_frames", "02_vggt", "03_sfm"
    }:
        raise ValueError("Output cannot be inside an input stage")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--colmap", default="colmap")
    parser.add_argument("--max-image-size", type=int, default=1600)
    parser.add_argument("--neighbors", type=int, default=12)
    parser.add_argument("--cache-gb", type=float, default=4.0)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--max-images", type=int, default=240)
    parser.add_argument("--min-points", type=int, default=500)
    parser.add_argument("--min-fused-points", type=int, default=1000)
    parser.add_argument("--min-observations", type=int, default=50)
    parser.add_argument("--min-shared-points", type=int, default=30)
    parser.add_argument("--max-median-error", type=float, default=2.0)
    parser.add_argument("--max-p95-error", type=float, default=4.0)
    parser.add_argument("--min-median-angle", type=float, default=1.5)
    parser.add_argument("--min-pair-angle", type=float, default=1.0)
    args = parser.parse_args()
    if not (320 <= args.max_image_size <= 2400 and 2 <= args.neighbors <= 16
            and 0.5 <= args.cache_gb <= 8 and 1 <= args.threads <= 8
            and 3 <= args.max_images <= 500):
        parser.error("Resource settings outside safe bounds (size 320..2400, neighbors 2..16, cache .5..8, threads 1..8, images 3..500)")
    if min(args.min_points, args.min_fused_points, args.min_observations, args.min_shared_points) < 1 or not all(
        math.isfinite(v) and v > 0 for v in (args.max_median_error, args.max_p95_error,
                                            args.min_median_angle, args.min_pair_angle)
    ):
        parser.error("Quality thresholds must be finite and positive")
    source, output = args.source.resolve(), args.output.resolve()
    try:
        validate_output_location(source, output)
    except ValueError as exc:
        parser.error(str(exc))
    if output.exists():
        parser.error(f"Refusing to overwrite existing output: {output}; choose a new scene")
    if not (source / "01_frames/images").is_dir() or not (source / "03_sfm/model").is_dir():
        parser.error("Source needs 01_frames/images and 03_sfm/model")
    import pycolmap  # Lazy: pure geometry/reader tests need no CUDA or COLMAP.
    reconstruction = pycolmap.Reconstruction(str(source / "03_sfm/model"))
    if len(reconstruction.images) > args.max_images:
        parser.error(f"Source exceeds the configured {args.max_images} image bound")
    output.mkdir(parents=True, exist_ok=False)
    (output / "logs").mkdir()
    status = {"status": "running", "source": str(source), "coordinate_frame": "source refined COLMAP model",
              "settings": vars(args) | {"source": str(source), "output": str(output)}}
    write_json(output / "summary.json", status)
    try:
        # BA does not guarantee serialized point.error values were refreshed.
        # This bound 3.11.1 method recomputes errors from current poses/tracks.
        reconstruction.update_point_3d_errors()
        report = quality_report(reconstruction, **{key: getattr(args, key) for key in (
            "neighbors", "min_points", "min_observations", "min_shared_points", "max_median_error",
            "max_p95_error", "min_median_angle", "min_pair_angle")})
        write_json(output / "quality_report.json", report)
        if not report["passed"]:
            raise RuntimeError("Sparse quality gates failed: " + "; ".join(report["failures"]))
        for image in reconstruction.images.values():
            relative = Path(image.name)
            if relative.is_absolute() or ".." in relative.parts or any(c in image.name for c in "\n\r,"):
                raise RuntimeError(f"Image name cannot be represented safely in stereo config: {image.name!r}")
            if not (source / "01_frames/images" / relative).is_file():
                raise RuntimeError(f"Missing source image: {image.name}")
        sparse_txt = output / "sparse_txt"
        sparse_txt.mkdir()
        reconstruction.write_text(str(sparse_txt))
        dense = output / "dense"
        run_command(output, "undistort", [args.colmap, "image_undistorter", "--image_path",
            str(source / "01_frames/images"), "--input_path", str(sparse_txt), "--output_path", str(dense),
            "--output_type", "COLMAP", "--max_image_size", str(args.max_image_size),
            "--num_patch_match_src_images", str(args.neighbors)])
        config = dense / "stereo/patch-match.cfg"
        config.write_text("".join(name + "\n" + ", ".join(other) + "\n"
                                  for name, other in report["source_neighbors"].items()))
        run_command(output, "patch_match", [args.colmap, "patch_match_stereo", "--workspace_path", str(dense),
            "--workspace_format", "COLMAP", "--PatchMatchStereo.gpu_index", "0",
            "--PatchMatchStereo.max_image_size", str(args.max_image_size),
            "--PatchMatchStereo.cache_size", str(args.cache_gb), "--PatchMatchStereo.geom_consistency", "1",
            "--PatchMatchStereo.filter", "1", "--PatchMatchStereo.filter_min_num_consistent", "2",
            "--PatchMatchStereo.filter_min_triangulation_angle", str(args.min_pair_angle)])
        run_command(output, "stereo_fusion", [args.colmap, "stereo_fusion", "--workspace_path", str(dense),
            "--workspace_format", "COLMAP", "--input_type", "geometric", "--output_path", str(dense / "fused.ply"),
            "--StereoFusion.min_num_pixels", "3", "--StereoFusion.max_image_size", str(args.max_image_size),
            "--StereoFusion.cache_size", str(args.cache_gb), "--StereoFusion.use_cache", "1",
            "--StereoFusion.check_num_images", str(args.neighbors), "--StereoFusion.num_threads", str(args.threads)])
        depth_report = []
        for name in report["source_neighbors"]:
            depth = read_colmap_array(dense / "stereo/depth_maps" / f"{name}.geometric.bin")
            if depth.ndim != 2:
                raise RuntimeError(f"Invalid depth map channels: {name}")
            valid = np.isfinite(depth) & (depth > 0)
            depth_report.append({"name": name, "width": depth.shape[1], "height": depth.shape[0],
                                 "valid_fraction": float(valid.mean())})
        fused_count = ply_vertex_count(dense / "fused.ply")
        status.update(fused_pointcloud=str(dense / "fused.ply"), fused_points=fused_count, depth_maps=depth_report)
        if fused_count < args.min_fused_points:
            raise RuntimeError(f"Stereo fusion produced only {fused_count} points; need {args.min_fused_points}")
        if any(v["valid_fraction"] < 0.001 for v in depth_report):
            raise RuntimeError("One or more views have fewer than 0.1% valid geometric pixels; inspect depth_maps in summary")
        status["status"] = "complete"
    except Exception as exc:
        status.update(status="failed", error=str(exc))
        write_json(output / "summary.json", status)
        raise
    write_json(output / "summary.json", status)
    print(f"Precision MVS complete: {output / 'dense/fused.ply'}", flush=True)


if __name__ == "__main__":
    main()
