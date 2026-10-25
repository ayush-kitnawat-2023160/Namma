#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from plyfile import PlyData, PlyElement


def decode_depth(path: Path, scale: float) -> np.ndarray:
    encoded = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if encoded is None:
        raise RuntimeError(f"Cannot read depth map: {path}")
    inv = encoded.astype(np.float32) / float(scale)
    return np.where(inv > 1e-8, 1.0 / inv, 0.0).astype(np.float32)


def camera_centers(extrinsics: np.ndarray) -> np.ndarray:
    centers = []
    for ext in extrinsics:
        R = ext[:3, :3]
        t = ext[:3, 3]
        centers.append(-R.T @ t)
    return np.asarray(centers, dtype=np.float32)


def build_neighbors(centers: np.ndarray, count: int) -> list[list[int]]:
    n = len(centers)
    distances = np.linalg.norm(centers[:, None, :] - centers[None, :, :], axis=2)
    np.fill_diagonal(distances, np.inf)
    result: list[list[int]] = []
    for i in range(n):
        spatial = [int(v) for v in np.argsort(distances[i])[:count]]
        # Always preserve immediate temporal overlap as well; spatially closest
        # views alone can miss a turning sequence or a short revisited section.
        temporal = [j for j in (i - 2, i - 1, i + 1, i + 2) if 0 <= j < n]
        merged: list[int] = []
        for j in temporal + spatial:
            if j != i and j not in merged:
                merged.append(j)
        result.append(merged[: max(count, 4)])
    return result


def unproject_grid(depth: np.ndarray, K: np.ndarray, ext: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    h, w = depth.shape
    yy, xx = np.meshgrid(np.arange(h, dtype=np.float32), np.arange(w, dtype=np.float32), indexing="ij")
    fx, fy = float(K[0, 0]), float(K[1, 1])
    cx, cy = float(K[0, 2]), float(K[1, 2])
    cam = np.stack(
        [
            (xx - cx) / fx * depth,
            (yy - cy) / fy * depth,
            depth,
        ],
        axis=-1,
    ).astype(np.float32)
    R = ext[:3, :3].astype(np.float32)
    t = ext[:3, 3].astype(np.float32)
    world = np.einsum("ij,hwj->hwi", R.T, cam - t[None, None, :])

    dx = np.zeros_like(cam)
    dy = np.zeros_like(cam)
    dx[:, 1:-1] = cam[:, 2:] - cam[:, :-2]
    dy[1:-1, :] = cam[2:, :] - cam[:-2, :]
    normals_cam = np.cross(dx, dy)
    norm = np.linalg.norm(normals_cam, axis=-1, keepdims=True)
    normalized_cam = np.zeros_like(normals_cam)
    np.divide(normals_cam, np.maximum(norm, 1e-8), out=normalized_cam, where=norm > 1e-8)
    normals_cam = normalized_cam
    normals_world = np.einsum("ij,hwj->hwi", R.T, normals_cam)
    invalid = (depth <= 1e-8) | ~np.isfinite(depth)
    normals_world[invalid] = 0.0
    return world, normals_world.astype(np.float32)


def load_rgb(vggt: Path, frames: Path, stem: str, size: tuple[int, int]) -> np.ndarray:
    model_image = vggt / "model_images" / f"{stem}.png"
    path = model_image if model_image.is_file() else frames / f"{stem}.png"
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"Cannot read RGB image for {stem}: {path}")
    w, h = size
    if image.shape[1] != w or image.shape[0] != h:
        image = cv2.resize(image, (w, h), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)


def save_ply(path: Path, xyz: np.ndarray, normal: np.ndarray, rgb: np.ndarray) -> None:
    dtype = [
        ("x", "f4"), ("y", "f4"), ("z", "f4"),
        ("nx", "f4"), ("ny", "f4"), ("nz", "f4"),
        ("red", "u1"), ("green", "u1"), ("blue", "u1"),
    ]
    vertices = np.empty(len(xyz), dtype=dtype)
    attrs = np.concatenate(
        [xyz.astype(np.float32), normal.astype(np.float32), rgb.astype(np.uint8)], axis=1
    )
    vertices[:] = list(map(tuple, attrs))
    path.parent.mkdir(parents=True, exist_ok=True)
    PlyData([PlyElement.describe(vertices, "vertex")], text=False).write(path)


def voxel_fuse(
    points: np.ndarray,
    normals: np.ndarray,
    colors: np.ndarray,
    weights: np.ndarray,
    supports: np.ndarray,
    voxel_size: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    origin = points.min(axis=0)
    voxels = np.floor((points - origin) / voxel_size).astype(np.int32)
    order = np.lexsort((voxels[:, 2], voxels[:, 1], voxels[:, 0]))
    sorted_vox = voxels[order]
    changed = np.empty(len(order), dtype=bool)
    changed[0] = True
    changed[1:] = np.any(sorted_vox[1:] != sorted_vox[:-1], axis=1)
    starts = np.flatnonzero(changed)

    w = weights[order].astype(np.float64)
    sum_w = np.add.reduceat(w, starts)
    p = np.add.reduceat(points[order].astype(np.float64) * w[:, None], starts, axis=0) / sum_w[:, None]
    c = np.add.reduceat(colors[order].astype(np.float64) * w[:, None], starts, axis=0) / sum_w[:, None]
    n = np.add.reduceat(normals[order].astype(np.float64) * w[:, None], starts, axis=0)
    n_norm = np.linalg.norm(n, axis=1, keepdims=True)
    normalized = np.zeros_like(n)
    np.divide(n, np.maximum(n_norm, 1e-8), out=normalized, where=n_norm > 1e-8)
    n = normalized
    s = np.maximum.reduceat(supports[order], starts)
    return (
        p.astype(np.float32),
        n.astype(np.float32),
        np.clip(c, 0, 255).astype(np.uint8),
        sum_w.astype(np.float32),
        s.astype(np.uint8),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Cross-view VGGT-Omega geometry validation and surfel fusion")
    parser.add_argument("--vggt", type=Path, required=True)
    parser.add_argument("--frames", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--neighbors", type=int, default=6)
    parser.add_argument("--relative-depth-threshold", type=float, default=0.04)
    parser.add_argument("--min-support", type=int, default=1)
    parser.add_argument("--pixel-stride", type=int, default=3)
    parser.add_argument("--confidence-percentile", type=float, default=30.0)
    parser.add_argument("--voxel-size", type=float, default=0.0)
    parser.add_argument("--max-points", type=int, default=2_000_000)
    args = parser.parse_args()

    if args.pixel_stride < 1 or args.neighbors < 1 or args.min_support < 0:
        raise RuntimeError("Invalid fusion sampling/support parameters")
    args.output.mkdir(parents=True, exist_ok=True)

    camera_data = np.load(args.vggt / "cameras.npz")
    names = [str(v) for v in camera_data["image_names"].tolist()]
    extrinsics = np.asarray(camera_data["extrinsic"], dtype=np.float32)
    intrinsics = np.asarray(camera_data["intrinsic"], dtype=np.float32)
    summary = json.loads((args.vggt / "summary.json").read_text())
    encode_scale = float(summary["inverse_depth_encode_scale"])

    if len(names) != len(extrinsics) or len(names) != len(intrinsics):
        raise RuntimeError("VGGT camera/image counts do not match")

    depths: list[np.ndarray] = []
    confidences: list[np.ndarray] = []
    conf_thresholds: list[float] = []
    for name in names:
        stem = Path(name).stem
        depth = decode_depth(args.vggt / "depths" / f"{stem}.png", encode_scale)
        conf = np.asarray(np.load(args.vggt / "confidence" / f"{stem}.npy"), dtype=np.float32)
        if conf.shape != depth.shape:
            conf = cv2.resize(conf, (depth.shape[1], depth.shape[0]), interpolation=cv2.INTER_LINEAR)
        good = conf[np.isfinite(conf) & (conf > 1e-8)]
        threshold = float(np.percentile(good, args.confidence_percentile)) if good.size else np.inf
        depths.append(depth)
        confidences.append(conf)
        conf_thresholds.append(threshold)

    centers = camera_centers(extrinsics)
    neighbors = build_neighbors(centers, args.neighbors)
    all_points: list[np.ndarray] = []
    all_normals: list[np.ndarray] = []
    all_colors: list[np.ndarray] = []
    all_weights: list[np.ndarray] = []
    all_supports: list[np.ndarray] = []
    candidate_count = 0
    accepted_count = 0

    print(
        f"Fusion: {len(names)} views, neighbors={args.neighbors}, rel_depth={args.relative_depth_threshold:.3f}, "
        f"min_support={args.min_support}, pixel_stride={args.pixel_stride}"
    )

    for i, name in enumerate(names):
        depth = depths[i]
        conf = confidences[i]
        h, w = depth.shape
        world, normals = unproject_grid(depth, intrinsics[i], extrinsics[i])
        yy = np.arange(1, h - 1, args.pixel_stride)
        xx = np.arange(1, w - 1, args.pixel_stride)
        gy, gx = np.meshgrid(yy, xx, indexing="ij")
        gy = gy.reshape(-1)
        gx = gx.reshape(-1)

        source_depth = depth[gy, gx]
        source_conf = conf[gy, gx]
        valid = (
            np.isfinite(source_depth)
            & (source_depth > 1e-6)
            & np.isfinite(source_conf)
            & (source_conf >= conf_thresholds[i])
        )
        gy, gx = gy[valid], gx[valid]
        if len(gx) == 0:
            continue
        points = world[gy, gx]
        point_normals = normals[gy, gx]
        source_conf = source_conf[valid]
        candidate_count += len(points)

        support = np.zeros(len(points), dtype=np.uint8)
        for j in neighbors[i]:
            ext = extrinsics[j]
            K = intrinsics[j]
            p_cam = points @ ext[:3, :3].T + ext[:3, 3]
            z = p_cam[:, 2]
            projected = z > 1e-6
            u = np.zeros(len(points), dtype=np.float32)
            v = np.zeros(len(points), dtype=np.float32)
            u[projected] = K[0, 0] * p_cam[projected, 0] / z[projected] + K[0, 2]
            v[projected] = K[1, 1] * p_cam[projected, 1] / z[projected] + K[1, 2]
            ui = np.rint(u).astype(np.int32)
            vi = np.rint(v).astype(np.int32)
            inside = projected & (ui >= 0) & (ui < depths[j].shape[1]) & (vi >= 0) & (vi < depths[j].shape[0])
            indices = np.flatnonzero(inside)
            if indices.size == 0:
                continue
            d_ref = depths[j][vi[indices], ui[indices]]
            c_ref = confidences[j][vi[indices], ui[indices]]
            rel = np.abs(z[indices] - d_ref) / np.maximum(d_ref, 1e-6)
            agreed = (
                np.isfinite(d_ref)
                & (d_ref > 1e-6)
                & np.isfinite(c_ref)
                & (c_ref >= conf_thresholds[j])
                & (rel <= args.relative_depth_threshold)
            )
            support[indices[agreed]] += 1

        keep = support >= args.min_support
        if not np.any(keep):
            print(f"  [{i+1:03d}/{len(names):03d}] {name}: 0/{len(points)} supported")
            continue

        rgb = load_rgb(args.vggt, args.frames, Path(name).stem, (w, h))[gy, gx]
        scale = max(conf_thresholds[i], 1e-6)
        confidence_weight = np.clip(source_conf / scale, 0.25, 4.0)
        weights = confidence_weight * (1.0 + support.astype(np.float32))
        accepted_count += int(keep.sum())
        all_points.append(points[keep])
        all_normals.append(point_normals[keep])
        all_colors.append(rgb[keep])
        all_weights.append(weights[keep].astype(np.float32))
        all_supports.append(support[keep])
        print(f"  [{i+1:03d}/{len(names):03d}] {name}: {int(keep.sum())}/{len(points)} supported")

    if not all_points:
        raise RuntimeError("Geometry fusion rejected every point; relax NG_FUSION_* thresholds")

    points = np.concatenate(all_points, axis=0)
    normals = np.concatenate(all_normals, axis=0)
    colors = np.concatenate(all_colors, axis=0)
    weights = np.concatenate(all_weights, axis=0)
    supports = np.concatenate(all_supports, axis=0)

    positive_chunks = [d[np.isfinite(d) & (d > 1e-6)] for d in depths]
    positive_chunks = [chunk for chunk in positive_chunks if chunk.size]
    if not positive_chunks:
        raise RuntimeError("No positive VGGT depth remained for voxel-size estimation")
    positive_depth = np.concatenate(positive_chunks)
    median_depth = float(np.median(positive_depth))
    voxel_size = float(args.voxel_size) if args.voxel_size > 0 else max(1e-4, median_depth * 0.003)
    print(f"Voxel fusion: {len(points)} supported samples, voxel_size={voxel_size:.6g}")
    points, normals, colors, weights, supports = voxel_fuse(
        points, normals, colors, weights, supports, voxel_size
    )

    if len(points) > args.max_points:
        # Keep the strongest geometrically supported surfels rather than uniform
        # index sampling, which can preserve weak floaters by chance.
        chosen = np.argpartition(weights, -args.max_points)[-args.max_points:]
        points = points[chosen]
        normals = normals[chosen]
        colors = colors[chosen]
        weights = weights[chosen]
        supports = supports[chosen]

    save_ply(args.output / "surface.ply", points, normals, colors)
    np.savez_compressed(
        args.output / "surface_metadata.npz",
        weight=weights.astype(np.float32),
        support=supports.astype(np.uint8),
        voxel_size=np.float32(voxel_size),
    )
    report = {
        "views": len(names),
        "candidate_samples": int(candidate_count),
        "cross_view_supported_samples": int(accepted_count),
        "cross_view_acceptance_fraction": float(accepted_count / max(candidate_count, 1)),
        "fused_surface_points": int(len(points)),
        "median_vggt_depth": median_depth,
        "voxel_size": voxel_size,
        "neighbors": args.neighbors,
        "relative_depth_threshold": args.relative_depth_threshold,
        "min_supporting_other_views": args.min_support,
        "pixel_stride": args.pixel_stride,
        "confidence_percentile": args.confidence_percentile,
        "coordinate_frame": "VGGT-Omega world; aligned to COLMAP in gs-dataset stage",
    }
    (args.output / "summary.json").write_text(json.dumps(report, indent=2))
    print("=== MULTI-VIEW GEOMETRY FUSION COMPLETE ===")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
