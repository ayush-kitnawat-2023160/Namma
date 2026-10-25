#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
import open3d as o3d
import trimesh

from fuse_geometry import camera_centers, decode_depth


def threshold_for_confidence(conf: np.ndarray, percentile: float) -> float:
    good = conf[np.isfinite(conf) & (conf > 1e-8)]
    return float(np.percentile(good, percentile)) if good.size else np.inf


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a visually useful same-geometry presentation mesh using multiview RGB color baking."
    )
    parser.add_argument("--truth-mesh", type=Path, required=True)
    parser.add_argument("--vggt", type=Path, required=True)
    parser.add_argument("--frames", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confidence-percentile", type=float, default=30.0)
    parser.add_argument("--depth-tolerance", type=float, default=0.05)
    parser.add_argument("--target-triangles", type=int, default=1_000_000)
    parser.add_argument("--chunk-size", type=int, default=250_000)
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    mesh = o3d.io.read_triangle_mesh(str(args.truth_mesh))
    if mesh.is_empty() or len(mesh.triangles) == 0:
        raise RuntimeError(f"Truth mesh is empty: {args.truth_mesh}")

    source_triangles = len(mesh.triangles)
    if args.target_triangles > 0 and source_triangles > args.target_triangles:
        print(f"Presentation decimation: {source_triangles} -> ~{args.target_triangles} triangles")
        mesh = mesh.simplify_quadric_decimation(args.target_triangles)
        mesh.remove_degenerate_triangles()
        mesh.remove_duplicated_triangles()
        mesh.remove_duplicated_vertices()
        mesh.remove_unreferenced_vertices()
    mesh.compute_vertex_normals()

    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    normals = np.asarray(mesh.vertex_normals, dtype=np.float64)
    faces = np.asarray(mesh.triangles, dtype=np.int64)
    if mesh.has_vertex_colors():
        fallback_rgb = np.clip(np.asarray(mesh.vertex_colors) * 255.0, 0, 255).astype(np.uint8)
    else:
        fallback_rgb = np.full((len(vertices), 3), 160, dtype=np.uint8)

    camera_data = np.load(args.vggt / "cameras.npz")
    names = [str(v) for v in camera_data["image_names"].tolist()]
    extrinsics = np.asarray(camera_data["extrinsic"], dtype=np.float64)
    intrinsics = np.asarray(camera_data["intrinsic"], dtype=np.float64)
    model_w = int(camera_data["model_width"])
    model_h = int(camera_data["model_height"])
    original_w = int(camera_data["original_width"])
    original_h = int(camera_data["original_height"])
    summary = json.loads((args.vggt / "summary.json").read_text())
    encode_scale = float(summary["inverse_depth_encode_scale"])

    sx = original_w / float(model_w)
    sy = original_h / float(model_h)
    centers = camera_centers(extrinsics.astype(np.float32)).astype(np.float64)

    best_score = np.full(len(vertices), -np.inf, dtype=np.float32)
    best_color = fallback_rgb.copy()
    colored_from_4k = np.zeros(len(vertices), dtype=bool)

    print(
        f"Presentation color bake: vertices={len(vertices)}, views={len(names)}, "
        f"model={model_w}x{model_h}, source={original_w}x{original_h}"
    )

    for camera_index, name in enumerate(names):
        stem = Path(name).stem
        depth = decode_depth(args.vggt / "depths" / f"{stem}.png", encode_scale)
        conf = np.asarray(np.load(args.vggt / "confidence" / f"{stem}.npy"), dtype=np.float32)
        if conf.shape != depth.shape:
            conf = cv2.resize(conf, (depth.shape[1], depth.shape[0]), interpolation=cv2.INTER_LINEAR)
        conf_threshold = threshold_for_confidence(conf, args.confidence_percentile)

        full_path = args.frames / name
        bgr = cv2.imread(str(full_path), cv2.IMREAD_COLOR)
        if bgr is None:
            raise RuntimeError(f"Cannot read selected full-resolution frame: {full_path}")
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        if rgb.shape[1] != original_w or rgb.shape[0] != original_h:
            raise RuntimeError(
                f"Unexpected selected-frame size for {name}: {rgb.shape[1]}x{rgb.shape[0]} "
                f"!= {original_w}x{original_h}"
            )

        ext = extrinsics[camera_index]
        K = intrinsics[camera_index]
        center = centers[camera_index]
        updated = 0

        for start in range(0, len(vertices), args.chunk_size):
            end = min(start + args.chunk_size, len(vertices))
            pts = vertices[start:end]
            nrms = normals[start:end]
            p_cam = pts @ ext[:3, :3].T + ext[:3, 3]
            z = p_cam[:, 2]
            valid_z = z > 1e-6
            u = np.zeros(len(pts), dtype=np.float64)
            v = np.zeros(len(pts), dtype=np.float64)
            u[valid_z] = K[0, 0] * p_cam[valid_z, 0] / z[valid_z] + K[0, 2]
            v[valid_z] = K[1, 1] * p_cam[valid_z, 1] / z[valid_z] + K[1, 2]
            ui = np.rint(u).astype(np.int32)
            vi = np.rint(v).astype(np.int32)
            inside = (
                valid_z
                & (ui >= 0)
                & (ui < model_w)
                & (vi >= 0)
                & (vi < model_h)
            )
            ids = np.flatnonzero(inside)
            if ids.size == 0:
                continue

            d_ref = depth[vi[ids], ui[ids]]
            c_ref = conf[vi[ids], ui[ids]]
            rel = np.abs(z[ids] - d_ref) / np.maximum(d_ref, 1e-6)
            visible = (
                np.isfinite(d_ref)
                & (d_ref > 1e-6)
                & np.isfinite(c_ref)
                & (c_ref >= conf_threshold)
                & (rel <= args.depth_tolerance)
            )
            ids = ids[visible]
            if ids.size == 0:
                continue

            view_dir = center[None, :] - pts[ids]
            view_norm = np.linalg.norm(view_dir, axis=1, keepdims=True)
            view_dir = view_dir / np.maximum(view_norm, 1e-9)
            angle = np.abs(np.sum(nrms[ids] * view_dir, axis=1))
            conf_value = conf[vi[ids], ui[ids]]
            conf_weight = np.clip(conf_value / max(conf_threshold, 1e-6), 0.25, 4.0)
            score = ((0.15 + 0.85 * angle) ** 2 * conf_weight).astype(np.float32)

            global_ids = start + ids
            improve = score > best_score[global_ids]
            if not np.any(improve):
                continue
            chosen_global = global_ids[improve]
            chosen_local = ids[improve]

            uf = np.clip(np.rint(u[chosen_local] * sx).astype(np.int32), 0, original_w - 1)
            vf = np.clip(np.rint(v[chosen_local] * sy).astype(np.int32), 0, original_h - 1)
            best_color[chosen_global] = rgb[vf, uf]
            best_score[chosen_global] = score[improve]
            colored_from_4k[chosen_global] = True
            updated += int(len(chosen_global))

        print(f"  [{camera_index+1:03d}/{len(names):03d}] {name}: improved {updated} vertices")

    mesh.vertex_colors = o3d.utility.Vector3dVector(best_color.astype(np.float64) / 255.0)
    ply_path = args.output / "presentation_mesh.ply"
    if not o3d.io.write_triangle_mesh(str(ply_path), mesh, write_ascii=False):
        raise RuntimeError(f"Failed to write {ply_path}")

    rgba = np.concatenate(
        [best_color.astype(np.uint8), np.full((len(best_color), 1), 255, dtype=np.uint8)], axis=1
    )
    tri = trimesh.Trimesh(
        vertices=np.asarray(mesh.vertices),
        faces=np.asarray(mesh.triangles),
        vertex_colors=rgba,
        process=False,
    )
    glb_path = args.output / "presentation_mesh.glb"
    glb_path.write_bytes(tri.export(file_type="glb"))

    report = {
        "representation": "verified_tsdf_geometry_with_multiview_4k_vertex_color_bake",
        "source_truth_mesh": str(args.truth_mesh),
        "source_triangles": int(source_triangles),
        "presentation_vertices": int(len(mesh.vertices)),
        "presentation_triangles": int(len(mesh.triangles)),
        "vertices_colored_from_full_resolution_views": int(colored_from_4k.sum()),
        "colored_fraction": float(colored_from_4k.mean()) if len(colored_from_4k) else 0.0,
        "depth_visibility_tolerance": args.depth_tolerance,
        "confidence_percentile": args.confidence_percentile,
        "ply": str(ply_path),
        "glb": str(glb_path),
        "notes": [
            "Geometry is inherited from the verified TSDF truth mesh.",
            "Each vertex selects its strongest visible 4K observation using depth, confidence, and view angle.",
            "GLB uses baked vertex colors for robust browser viewing; SuGaR separately provides a conventional UV-textured OBJ.",
        ],
    }
    (args.output / "presentation_summary.json").write_text(json.dumps(report, indent=2))
    print("=== PRESENTATION MESH COMPLETE ===")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
