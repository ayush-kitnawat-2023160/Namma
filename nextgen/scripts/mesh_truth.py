#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
import open3d as o3d

from fuse_geometry import build_neighbors, camera_centers, decode_depth, unproject_grid


def confidence_threshold(conf: np.ndarray, percentile: float) -> float:
    good = conf[np.isfinite(conf) & (conf > 1e-8)]
    return float(np.percentile(good, percentile)) if good.size else np.inf


def supported_depth_mask(
    index: int,
    depths: list[np.ndarray],
    confidences: list[np.ndarray],
    thresholds: list[float],
    intrinsics: np.ndarray,
    extrinsics: np.ndarray,
    neighbors: list[list[int]],
    relative_depth_threshold: float,
    min_support: int,
) -> tuple[np.ndarray, np.ndarray]:
    depth = depths[index]
    conf = confidences[index]
    h, w = depth.shape
    world, _ = unproject_grid(depth, intrinsics[index], extrinsics[index])

    valid = (
        np.isfinite(depth)
        & (depth > 1e-6)
        & np.isfinite(conf)
        & (conf >= thresholds[index])
    )
    flat_indices = np.flatnonzero(valid.reshape(-1))
    support = np.zeros(h * w, dtype=np.uint8)
    if flat_indices.size == 0:
        return valid & False, support.reshape(h, w)

    points = world.reshape(-1, 3)[flat_indices]
    for j in neighbors[index]:
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
        inside = (
            projected
            & (ui >= 0)
            & (ui < depths[j].shape[1])
            & (vi >= 0)
            & (vi < depths[j].shape[0])
        )
        ids = np.flatnonzero(inside)
        if ids.size == 0:
            continue
        d_ref = depths[j][vi[ids], ui[ids]]
        c_ref = confidences[j][vi[ids], ui[ids]]
        rel = np.abs(z[ids] - d_ref) / np.maximum(d_ref, 1e-6)
        agreed = (
            np.isfinite(d_ref)
            & (d_ref > 1e-6)
            & np.isfinite(c_ref)
            & (c_ref >= thresholds[j])
            & (rel <= relative_depth_threshold)
        )
        supported_local = ids[agreed]
        if supported_local.size:
            support[flat_indices[supported_local]] += 1

    support_2d = support.reshape(h, w)
    keep = valid & (support_2d >= min_support)
    return keep, support_2d


def clean_mesh(
    mesh: o3d.geometry.TriangleMesh,
    min_component_triangles: int,
    min_component_fraction: float,
) -> tuple[o3d.geometry.TriangleMesh, dict]:
    cleaned = o3d.geometry.TriangleMesh(mesh)
    cleaned.remove_duplicated_vertices()
    cleaned.remove_duplicated_triangles()
    cleaned.remove_degenerate_triangles()
    cleaned.remove_unreferenced_vertices()

    component_report = {
        "components_before": 0,
        "largest_component_triangles": 0,
        "component_triangle_threshold": 0,
        "removed_small_component_triangles": 0,
    }
    if len(cleaned.triangles):
        labels, counts, _ = cleaned.cluster_connected_triangles()
        labels = np.asarray(labels, dtype=np.int64)
        counts = np.asarray(counts, dtype=np.int64)
        component_report["components_before"] = int(len(counts))
        if counts.size:
            largest = int(counts.max())
            threshold = max(
                int(min_component_triangles),
                int(np.ceil(largest * float(min_component_fraction))),
            )
            per_triangle_count = counts[labels]
            remove = per_triangle_count < threshold
            component_report["largest_component_triangles"] = largest
            component_report["component_triangle_threshold"] = threshold
            component_report["removed_small_component_triangles"] = int(remove.sum())
            if np.any(remove):
                cleaned.remove_triangles_by_mask(remove)
                cleaned.remove_unreferenced_vertices()

    cleaned.compute_vertex_normals()
    return cleaned, component_report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a conservative cross-view-verified TSDF truth mesh from VGGT-Omega depth."
    )
    parser.add_argument("--vggt", type=Path, required=True)
    parser.add_argument("--geometry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--neighbors", type=int, default=8)
    parser.add_argument("--relative-depth-threshold", type=float, default=0.03)
    parser.add_argument("--min-support", type=int, default=2)
    parser.add_argument("--confidence-percentile", type=float, default=30.0)
    parser.add_argument("--voxel-scale", type=float, default=1.0)
    parser.add_argument("--sdf-trunc-voxels", type=float, default=5.0)
    parser.add_argument("--depth-percentile", type=float, default=99.5)
    parser.add_argument("--min-component-triangles", type=int, default=100)
    parser.add_argument("--min-component-fraction", type=float, default=0.0002)
    parser.add_argument("--save-support-masks", action="store_true")
    args = parser.parse_args()

    if args.min_support < 1 or args.neighbors < 1:
        raise RuntimeError("neighbors/min-support must be positive")
    if args.voxel_scale <= 0 or args.sdf_trunc_voxels <= 0:
        raise RuntimeError("voxel/truncation scales must be positive")

    args.output.mkdir(parents=True, exist_ok=True)
    camera_data = np.load(args.vggt / "cameras.npz")
    names = [str(v) for v in camera_data["image_names"].tolist()]
    extrinsics = np.asarray(camera_data["extrinsic"], dtype=np.float64)
    intrinsics = np.asarray(camera_data["intrinsic"], dtype=np.float64)
    vggt_summary = json.loads((args.vggt / "summary.json").read_text())
    geometry_summary = json.loads((args.geometry / "summary.json").read_text())
    encode_scale = float(vggt_summary["inverse_depth_encode_scale"])

    depths: list[np.ndarray] = []
    confidences: list[np.ndarray] = []
    thresholds: list[float] = []
    for name in names:
        stem = Path(name).stem
        depth = decode_depth(args.vggt / "depths" / f"{stem}.png", encode_scale)
        conf = np.asarray(np.load(args.vggt / "confidence" / f"{stem}.npy"), dtype=np.float32)
        if conf.shape != depth.shape:
            conf = cv2.resize(conf, (depth.shape[1], depth.shape[0]), interpolation=cv2.INTER_LINEAR)
        depths.append(depth)
        confidences.append(conf)
        thresholds.append(confidence_threshold(conf, args.confidence_percentile))

    positive = np.concatenate([d[np.isfinite(d) & (d > 1e-6)] for d in depths])
    # Open3D treats samples at/above depth_trunc as invalid. Leave a small
    # margin above the requested robust percentile so surfaces exactly on the
    # percentile boundary are not accidentally discarded.
    depth_trunc = float(np.percentile(positive, args.depth_percentile)) * 1.05
    base_voxel = float(geometry_summary["voxel_size"])
    voxel_size = base_voxel * float(args.voxel_scale)
    sdf_trunc = voxel_size * float(args.sdf_trunc_voxels)

    print(
        "Truth TSDF: "
        f"views={len(names)}, neighbors={args.neighbors}, min_support={args.min_support}, "
        f"rel_depth={args.relative_depth_threshold:.4f}, voxel={voxel_size:.6g}, "
        f"sdf_trunc={sdf_trunc:.6g}, depth_trunc={depth_trunc:.6g}"
    )

    centers = camera_centers(extrinsics.astype(np.float32))
    neighbors = build_neighbors(centers, args.neighbors)
    volume = o3d.pipelines.integration.ScalableTSDFVolume(
        voxel_length=voxel_size,
        sdf_trunc=sdf_trunc,
        color_type=o3d.pipelines.integration.TSDFVolumeColorType.RGB8,
    )

    masks_dir = args.output / "support_masks"
    if args.save_support_masks:
        masks_dir.mkdir(exist_ok=True)

    accepted_total = 0
    candidate_total = 0
    per_view: list[dict] = []
    for i, name in enumerate(names):
        keep, support = supported_depth_mask(
            i,
            depths,
            confidences,
            thresholds,
            intrinsics,
            extrinsics,
            neighbors,
            args.relative_depth_threshold,
            args.min_support,
        )
        filtered_depth = np.where(keep, depths[i], 0.0).astype(np.float32)
        candidates = int(
            np.count_nonzero(
                np.isfinite(depths[i])
                & (depths[i] > 1e-6)
                & np.isfinite(confidences[i])
                & (confidences[i] >= thresholds[i])
            )
        )
        accepted = int(np.count_nonzero(keep))
        candidate_total += candidates
        accepted_total += accepted

        stem = Path(name).stem
        color_path = args.vggt / "model_images" / f"{stem}.png"
        color_bgr = cv2.imread(str(color_path), cv2.IMREAD_COLOR)
        if color_bgr is None:
            raise RuntimeError(f"Missing pixel-aligned VGGT RGB image: {color_path}")
        if color_bgr.shape[:2] != filtered_depth.shape:
            raise RuntimeError(
                f"RGB/depth size mismatch for {name}: {color_bgr.shape[:2]} vs {filtered_depth.shape}"
            )
        color_rgb = cv2.cvtColor(color_bgr, cv2.COLOR_BGR2RGB)

        color_o3d = o3d.geometry.Image(np.ascontiguousarray(color_rgb))
        depth_o3d = o3d.geometry.Image(np.ascontiguousarray(filtered_depth))
        rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
            color_o3d,
            depth_o3d,
            depth_scale=1.0,
            depth_trunc=depth_trunc,
            convert_rgb_to_intensity=False,
        )
        K = intrinsics[i]
        h, w = filtered_depth.shape
        intrinsic = o3d.camera.PinholeCameraIntrinsic(
            w,
            h,
            float(K[0, 0]),
            float(K[1, 1]),
            float(K[0, 2]),
            float(K[1, 2]),
        )
        ext4 = np.eye(4, dtype=np.float64)
        ext4[:3, :4] = extrinsics[i][:3, :4]
        volume.integrate(rgbd, intrinsic, ext4)

        if args.save_support_masks:
            vis = np.clip(support.astype(np.uint16) * 50, 0, 255).astype(np.uint8)
            cv2.imwrite(str(masks_dir / f"{stem}.png"), vis)

        per_view.append({"name": name, "candidate_pixels": candidates, "accepted_pixels": accepted})
        print(f"  [{i+1:03d}/{len(names):03d}] {name}: {accepted}/{candidates} verified pixels")

    print("Extracting TSDF triangle mesh...")
    raw_mesh = volume.extract_triangle_mesh()
    raw_mesh.compute_vertex_normals()
    raw_path = args.output / "truth_tsdf_raw.ply"
    if not o3d.io.write_triangle_mesh(str(raw_path), raw_mesh, write_ascii=False):
        raise RuntimeError(f"Failed to write {raw_path}")

    clean, component_report = clean_mesh(
        raw_mesh,
        args.min_component_triangles,
        args.min_component_fraction,
    )
    clean_path = args.output / "truth_tsdf.ply"
    if not o3d.io.write_triangle_mesh(str(clean_path), clean, write_ascii=False):
        raise RuntimeError(f"Failed to write {clean_path}")

    summary = {
        "representation": "cross_view_verified_tsdf_truth_mesh",
        "views": len(names),
        "neighbors": args.neighbors,
        "min_supporting_other_views": args.min_support,
        "relative_depth_threshold": args.relative_depth_threshold,
        "confidence_percentile": args.confidence_percentile,
        "candidate_pixels": candidate_total,
        "accepted_pixels": accepted_total,
        "acceptance_fraction": float(accepted_total / max(candidate_total, 1)),
        "voxel_size": voxel_size,
        "sdf_trunc": sdf_trunc,
        "depth_trunc": depth_trunc,
        "raw_vertices": int(len(raw_mesh.vertices)),
        "raw_triangles": int(len(raw_mesh.triangles)),
        "clean_vertices": int(len(clean.vertices)),
        "clean_triangles": int(len(clean.triangles)),
        **component_report,
        "raw_mesh": str(raw_path),
        "truth_mesh": str(clean_path),
        "notes": [
            "No smoothing or large-hole filling is applied.",
            "Only confidence-filtered pixels with cross-view depth support are integrated.",
            "The cleaned mesh removes only tiny disconnected components and degenerate/duplicate topology.",
        ],
        "per_view": per_view,
    }
    (args.output / "truth_summary.json").write_text(json.dumps(summary, indent=2))
    print("=== VERIFIED TSDF TRUTH MESH COMPLETE ===")
    print(json.dumps({k: v for k, v in summary.items() if k != "per_view"}, indent=2))


if __name__ == "__main__":
    main()
