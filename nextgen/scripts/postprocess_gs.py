#!/usr/bin/env python3
"""Conservatively remove isolated 3DGS floaters while preserving PLY attributes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
import cv2
from plyfile import PlyData
from scipy.spatial import cKDTree


def load_xyz(path: Path, label: str) -> np.ndarray:
    ply = PlyData.read(str(path))
    if "vertex" not in ply:
        raise RuntimeError(f"{label} PLY has no vertex element: {path}")
    vertex = ply["vertex"].data
    missing = {"x", "y", "z"} - set(vertex.dtype.names or ())
    if missing:
        raise RuntimeError(f"{label} PLY is missing position fields: {sorted(missing)}")
    return np.column_stack((vertex["x"], vertex["y"], vertex["z"])).astype(np.float64)


def chunks(length: int, size: int):
    for start in range(0, length, size):
        yield start, min(start + size, length)


def score_depth_conflicts(
    scene_root: Path,
    positions: np.ndarray,
    chunk_size: int,
    nearest_view_count: int,
    confidence_percentile: float,
    relative_tolerance: float,
    edge_threshold: float,
    min_observations: int,
    min_conflicts: int,
) -> tuple[np.ndarray, np.ndarray, dict[str, object]] | None:
    """Find repeated free-space contradictions against reliable VGGT depth.

    The saved Sim(3) maps VGGT world coordinates into the COLMAP/model frame.
    Points are mapped back before projection so each depth map stays paired with
    the camera that produced it. Only splats in front of an observed surface are
    counted as conflicts; splats behind it may simply be occluded background.
    """
    vggt_dir = scene_root / "02_vggt"
    dataset_dir = scene_root / "05_gs_dataset"
    camera_file = vggt_dir / "cameras.npz"
    summary_file = vggt_dir / "summary.json"
    alignment_file = dataset_dir / "vggt_to_colmap_alignment.json"
    required = [camera_file, summary_file, alignment_file]
    if not all(path.is_file() for path in required):
        return None

    cameras = np.load(camera_file)
    names = [str(name) for name in cameras["image_names"].tolist()]
    extrinsics = np.asarray(cameras["extrinsic"], dtype=np.float64)
    intrinsics = np.asarray(cameras["intrinsic"], dtype=np.float64)
    if not names or len(names) != len(extrinsics) or len(names) != len(intrinsics):
        raise RuntimeError("VGGT camera/image counts do not match for depth-aware cleanup")

    alignment = json.loads(alignment_file.read_text())
    if not alignment.get("used_dense_vggt_initialization"):
        return None
    scale = float(alignment["scale"])
    rotation = np.asarray(alignment["rotation"], dtype=np.float64)
    translation = np.asarray(alignment["translation"], dtype=np.float64)
    if not np.isfinite(scale) or scale <= 0 or rotation.shape != (3, 3) or translation.shape != (3,):
        raise RuntimeError("Invalid VGGT-to-COLMAP alignment for depth-aware cleanup")

    vggt_centers = np.asarray(
        [-ext[:3, :3].T @ ext[:3, 3] for ext in extrinsics], dtype=np.float64
    )
    model_centers = scale * (vggt_centers @ rotation.T) + translation
    camera_tree = cKDTree(model_centers)
    view_count = min(nearest_view_count, len(names))
    selected_views = np.empty((len(positions), view_count), dtype=np.uint16)
    for start, end in chunks(len(positions), chunk_size):
        _, nearby = camera_tree.query(positions[start:end], k=view_count, workers=-1)
        if view_count == 1:
            nearby = nearby[:, None]
        selected_views[start:end] = nearby.astype(np.uint16)

    vggt_summary = json.loads(summary_file.read_text())
    depth_scale = float(vggt_summary["inverse_depth_encode_scale"])
    if depth_scale <= 0:
        raise RuntimeError("Invalid inverse-depth encoding scale")

    confidence_cutoffs = []
    for name in names:
        conf_path = vggt_dir / "confidence" / f"{Path(name).stem}.npy"
        if not conf_path.is_file():
            confidence_cutoffs.append(np.inf)
            continue
        confidence = np.asarray(np.load(conf_path), dtype=np.float32).squeeze()
        valid = confidence[np.isfinite(confidence) & (confidence > 1e-8)]
        confidence_cutoffs.append(
            float(np.percentile(valid, confidence_percentile)) if valid.size else np.inf
        )

    observations = np.zeros(len(positions), dtype=np.uint16)
    conflicts = np.zeros(len(positions), dtype=np.uint16)
    model_to_vggt_rotation = rotation
    for view_index, name in enumerate(names):
        indices = np.flatnonzero(np.any(selected_views == view_index, axis=1))
        if indices.size == 0 or not np.isfinite(confidence_cutoffs[view_index]):
            continue
        depth_path = vggt_dir / "depths" / f"{Path(name).stem}.png"
        conf_path = vggt_dir / "confidence" / f"{Path(name).stem}.npy"
        encoded = cv2.imread(str(depth_path), cv2.IMREAD_UNCHANGED)
        if encoded is None or not conf_path.is_file():
            continue
        inverse_depth = encoded.astype(np.float32) / depth_scale
        depth = np.divide(1.0, inverse_depth, out=np.zeros_like(inverse_depth), where=inverse_depth > 1e-8)
        confidence = np.asarray(np.load(conf_path), dtype=np.float32).squeeze()
        if confidence.shape != depth.shape:
            confidence = cv2.resize(confidence, (depth.shape[1], depth.shape[0]), interpolation=cv2.INTER_LINEAR)

        # Undo the saved similarity transform, then project through the original
        # VGGT camera. This avoids pretending the post-BA COLMAP poses generated
        # the raw VGGT depth maps.
        points_vggt = ((positions[indices] - translation) @ model_to_vggt_rotation) / scale
        ext = extrinsics[view_index]
        points_camera = points_vggt @ ext[:3, :3].T + ext[:3, 3]
        z = points_camera[:, 2]
        K = intrinsics[view_index]
        finite_projection = np.isfinite(points_camera).all(axis=1) & (z > 1e-6)
        u_float = np.full(len(indices), np.nan, dtype=np.float64)
        v_float = np.full(len(indices), np.nan, dtype=np.float64)
        projected_indices = np.flatnonzero(finite_projection)
        u_float[projected_indices] = (
            K[0, 0] * points_camera[projected_indices, 0] / z[projected_indices] + K[0, 2]
        )
        v_float[projected_indices] = (
            K[1, 1] * points_camera[projected_indices, 1] / z[projected_indices] + K[1, 2]
        )
        u_rounded = np.rint(u_float)
        v_rounded = np.rint(v_float)
        h, w = depth.shape
        inside = (
            finite_projection & np.isfinite(u_float) & np.isfinite(v_float)
            & (u_rounded >= 1) & (u_rounded < w - 1)
            & (v_rounded >= 1) & (v_rounded < h - 1)
        )
        local = np.flatnonzero(inside)
        if local.size == 0:
            continue
        px, py = u_rounded[local].astype(np.int32), v_rounded[local].astype(np.int32)
        ref_depth = depth[py, px]
        ref_conf = confidence[py, px]
        valid = (
            np.isfinite(ref_depth) & (ref_depth > 1e-6)
            & np.isfinite(ref_conf) & (ref_conf >= confidence_cutoffs[view_index])
        )
        # Ignore depth edges, where a small pose/pixel error can switch surfaces.
        neighborhood = np.stack(
            [depth[py + dy, px + dx] for dy in (-1, 0, 1) for dx in (-1, 0, 1)], axis=1
        )
        neighborhood_valid = np.isfinite(neighborhood) & (neighborhood > 1e-6)
        neighborhood_min = np.min(np.where(neighborhood_valid, neighborhood, np.inf), axis=1)
        neighborhood_max = np.max(np.where(neighborhood_valid, neighborhood, -np.inf), axis=1)
        smooth = (neighborhood_max - neighborhood_min) / np.maximum(ref_depth, 1e-6) <= edge_threshold
        usable = valid & smooth
        if not np.any(usable):
            continue
        point_indices = indices[local[usable]]
        observations[point_indices] += 1
        free_space_conflict = z[local[usable]] < ref_depth[usable] * (1.0 - relative_tolerance)
        conflicts[point_indices[free_space_conflict]] += 1

    contradiction_mask = (
        (observations >= min_observations)
        & (conflicts >= min_conflicts)
        & (conflicts.astype(np.float32) / np.maximum(observations, 1) >= 0.75)
    )
    stats = {
        "enabled": True,
        "views": len(names),
        "nearest_views_per_splat": view_count,
        "min_observations": min_observations,
        "min_conflicts": min_conflicts,
        "relative_tolerance": relative_tolerance,
        "confidence_percentile": confidence_percentile,
        "depth_edge_threshold": edge_threshold,
        "repeated_free_space_conflict_candidates": int(contradiction_mask.sum()),
        "splats_with_reliable_observations": int((observations > 0).sum()),
        "alignment_camera_center_rms": alignment.get("camera_center_rms_after_alignment"),
    }
    return contradiction_mask, conflicts, stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Write a separate cleaned Gaussian PLY. A splat is removed only when "
            "it is both far from the aligned fused surface and locally isolated."
        )
    )
    parser.add_argument("scene", help="Scene name under outputs/nextgen")
    parser.add_argument("--iteration", type=int, help="Model iteration (defaults to latest)")
    parser.add_argument("--input", type=Path, help="Override the trained Gaussian PLY")
    parser.add_argument("--surface", type=Path, help="Override aligned initializer surface PLY")
    parser.add_argument("--output", type=Path, help="Output PLY (defaults to 08_postprocess/cleaned.ply)")
    parser.add_argument("--surface-distance-factor", type=float, default=12.0)
    parser.add_argument("--isolation-radius-factor", type=float, default=6.0)
    parser.add_argument("--min-neighbors", type=int, default=1)
    parser.add_argument("--max-surface-samples", type=int, default=150_000)
    parser.add_argument("--chunk-size", type=int, default=100_000)
    parser.add_argument("--min-opacity", type=float, help="Optional extra opacity cutoff in [0, 1]")
    parser.add_argument("--disable-depth-check", action="store_true", help="Skip multi-view depth contradiction filtering")
    parser.add_argument("--depth-nearest-views", type=int, default=12)
    parser.add_argument("--depth-min-observations", type=int, default=6)
    parser.add_argument("--depth-min-conflicts", type=int, default=5)
    parser.add_argument("--depth-relative-tolerance", type=float, default=0.04)
    parser.add_argument("--depth-confidence-percentile", type=float, default=30.0)
    parser.add_argument("--depth-edge-threshold", type=float, default=0.06)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    if args.surface_distance_factor <= 0 or args.isolation_radius_factor <= 0:
        raise RuntimeError("Distance factors must be positive")
    if args.min_neighbors < 0 or args.max_surface_samples < 2 or args.chunk_size < 1:
        raise RuntimeError("Invalid neighbor or sampling count")
    if args.min_opacity is not None and not 0 <= args.min_opacity <= 1:
        raise RuntimeError("--min-opacity must be between 0 and 1")
    if args.depth_nearest_views < 1 or args.depth_min_observations < 1 or args.depth_min_conflicts < 1:
        raise RuntimeError("Depth view and vote counts must be positive")
    if not 0 <= args.depth_confidence_percentile <= 100:
        raise RuntimeError("--depth-confidence-percentile must be between 0 and 100")
    if args.depth_relative_tolerance <= 0 or args.depth_edge_threshold <= 0:
        raise RuntimeError("Depth tolerances must be positive")

    scene_root = Path("/workspace/outputs/nextgen") / args.scene
    model_root = scene_root / "06_gs_model/point_cloud"
    if args.input:
        input_path = args.input
    else:
        if args.iteration is None:
            iterations = []
            if model_root.is_dir():
                for candidate in model_root.glob("iteration_*"):
                    try:
                        iterations.append((int(candidate.name.removeprefix("iteration_")), candidate))
                    except ValueError:
                        continue
            if not iterations:
                raise RuntimeError(f"No trained iterations found under {model_root}")
            iteration, iteration_dir = max(iterations, key=lambda item: item[0])
        else:
            iteration = args.iteration
            iteration_dir = model_root / f"iteration_{iteration}"
        input_path = iteration_dir / "point_cloud.ply"
    surface_path = args.surface or scene_root / "05_gs_dataset/sparse/0/points3D.ply"
    output_path = args.output or scene_root / "08_postprocess/cleaned.ply"
    report_path = output_path.with_suffix(".report.json")

    if not input_path.is_file():
        raise RuntimeError(f"Gaussian PLY not found: {input_path}")
    if not surface_path.is_file():
        raise RuntimeError(
            f"Aligned fused surface not found: {surface_path}. Build the GS dataset first, "
            "or pass --surface with a surface in the trained model's coordinate frame."
        )
    if output_path.exists() or report_path.exists():
        if not args.overwrite:
            raise RuntimeError(f"Output already exists; pass --overwrite to replace it: {output_path}")

    model = PlyData.read(str(input_path))
    if "vertex" not in model:
        raise RuntimeError(f"Gaussian PLY has no vertex element: {input_path}")
    vertex = model["vertex"].data
    names = set(vertex.dtype.names or ())
    required = {"x", "y", "z", "opacity"}
    missing = required - names
    if missing:
        raise RuntimeError(f"Gaussian PLY is missing fields: {sorted(missing)}")

    positions = np.column_stack((vertex["x"], vertex["y"], vertex["z"])).astype(np.float64)
    finite = np.isfinite(positions).all(axis=1)
    for property_name in vertex.dtype.names or ():
        property_values = vertex[property_name]
        if property_values.dtype.kind == "f":
            property_finite = np.isfinite(property_values)
            if property_finite.ndim > 1:
                property_finite = property_finite.all(axis=tuple(range(1, property_finite.ndim)))
            finite &= property_finite
    if not np.any(finite):
        raise RuntimeError("No finite Gaussian positions and opacities found")

    surface = load_xyz(surface_path, "Aligned surface")
    surface = surface[np.isfinite(surface).all(axis=1)]
    if len(surface) < 2:
        raise RuntimeError("Aligned surface needs at least two finite points")
    if len(surface) > args.max_surface_samples:
        indices = np.linspace(0, len(surface) - 1, args.max_surface_samples, dtype=np.int64)
        surface = surface[indices]

    surface_tree = cKDTree(surface)
    surface_spacing = np.empty(len(surface), dtype=np.float64)
    for start, end in chunks(len(surface), args.chunk_size):
        distances, _ = surface_tree.query(surface[start:end], k=2, workers=-1)
        surface_spacing[start:end] = distances[:, 1]
    reference_spacing = float(np.median(surface_spacing[np.isfinite(surface_spacing) & (surface_spacing > 0)]))
    if not np.isfinite(reference_spacing) or reference_spacing <= 0:
        raise RuntimeError("Could not estimate positive surface point spacing")

    surface_cutoff = reference_spacing * args.surface_distance_factor
    isolation_radius = reference_spacing * args.isolation_radius_factor
    gaussian_tree = cKDTree(positions[finite])
    finite_indices = np.flatnonzero(finite)
    far_from_surface = np.zeros(len(vertex), dtype=bool)
    isolated = np.zeros(len(vertex), dtype=bool)
    nearest_surface = np.full(len(vertex), np.nan, dtype=np.float32)
    for start, end in chunks(len(finite_indices), args.chunk_size):
        indices = finite_indices[start:end]
        points = positions[indices]
        surface_dist, _ = surface_tree.query(points, k=1, workers=-1)
        counts = gaussian_tree.query_ball_point(points, isolation_radius, return_length=True, workers=-1)
        nearest_surface[indices] = surface_dist.astype(np.float32)
        far_from_surface[indices] = surface_dist > surface_cutoff
        isolated[indices] = (counts - 1) < args.min_neighbors

    geometry_outlier = finite & far_from_surface & isolated
    depth_conflict = np.zeros(len(vertex), dtype=bool)
    depth_conflict_votes = np.zeros(len(vertex), dtype=np.uint16)
    depth_stats: dict[str, object] = {"enabled": False, "reason": "disabled by option"}
    if not args.disable_depth_check:
        scored = score_depth_conflicts(
            scene_root,
            positions[finite_indices],
            args.chunk_size,
            args.depth_nearest_views,
            args.depth_confidence_percentile,
            args.depth_relative_tolerance,
            args.depth_edge_threshold,
            args.depth_min_observations,
            args.depth_min_conflicts,
        )
        if scored is None:
            depth_stats = {"enabled": False, "reason": "VGGT cameras or alignment metadata unavailable"}
        else:
            local_conflict, local_votes, depth_stats = scored
            depth_conflict[finite_indices] = local_conflict
            depth_conflict_votes[finite_indices] = local_votes

    low_opacity = np.zeros(len(vertex), dtype=bool)
    if args.min_opacity is not None:
        logits = np.asarray(vertex["opacity"], dtype=np.float64)
        opacity = np.empty_like(logits)
        opacity[logits >= 0] = 1.0 / (1.0 + np.exp(-logits[logits >= 0]))
        exp_logits = np.exp(logits[logits < 0])
        opacity[logits < 0] = exp_logits / (1.0 + exp_logits)
        low_opacity = finite & (opacity < args.min_opacity)

    keep = finite & ~geometry_outlier & ~depth_conflict & ~low_opacity
    if not np.any(keep):
        raise RuntimeError("Cleanup would remove every Gaussian; refusing to write an empty model")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f".{output_path.name}.tmp")
    try:
        model["vertex"].data = vertex[keep]
        model.write(str(temp_path))
        os.replace(temp_path, output_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    report = {
        "input_ply": str(input_path),
        "surface_ply": str(surface_path),
        "output_ply": str(output_path),
        "input_gaussians": int(len(vertex)),
        "output_gaussians": int(keep.sum()),
        "removed_gaussians": int((~keep).sum()),
        "removed_fraction": float((~keep).sum() / max(1, len(vertex))),
        "nonfinite_gaussians": int((~finite).sum()),
        "far_from_surface_candidates": int((finite & far_from_surface).sum()),
        "isolated_candidates": int((finite & isolated).sum()),
        "geometry_outliers_removed": int(geometry_outlier.sum()),
        "depth_conflict_candidates": int(depth_conflict.sum()),
        "depth_conflict_removed": int((depth_conflict & ~geometry_outlier & ~low_opacity).sum()),
        "depth_conflict_votes_percentiles": {
            str(percentile): int(value)
            for percentile, value in zip(
                (50, 90, 95, 99),
                np.percentile(depth_conflict_votes[finite], (50, 90, 95, 99)),
            )
        },
        "depth_check": depth_stats,
        "low_opacity_candidates": int(low_opacity.sum()),
        "low_opacity_removed": int((low_opacity & ~keep).sum()),
        "removed_category_counts_overlap": True,
        "vertex_properties_preserved": list(vertex.dtype.names or ()),
        "nearest_surface_distance_percentiles": {
            str(percentile): float(value)
            for percentile, value in zip(
                (50, 90, 95, 99),
                np.percentile(nearest_surface[finite], (50, 90, 95, 99)),
            )
        },
        "min_neighbors": args.min_neighbors,
        "reference_surface_spacing": reference_spacing,
        "surface_distance_cutoff": surface_cutoff,
        "isolation_radius": isolation_radius,
        "min_opacity": args.min_opacity,
        "surface_samples_used": int(len(surface)),
        "limitations": (
            "Distance, isolation, and repeated free-space depth contradictions are conservative "
            "proxies. Depth checks use raw VGGT cameras mapped by the saved Sim(3), ignore low "
            "confidence and depth edges, and only count splats in front of observed surfaces. "
            "Pose/depth errors can still cause false positives; review A/B renders."
        ),
    }
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
