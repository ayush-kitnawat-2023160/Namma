#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pycolmap
from plyfile import PlyData, PlyElement


def projection_center(image) -> np.ndarray:
    if hasattr(image, "projection_center"):
        value = image.projection_center
        value = value() if callable(value) else value
        return np.asarray(value, dtype=np.float64).reshape(3)

    pose = image.cam_from_world
    pose = pose() if callable(pose) else pose
    if hasattr(pose, "matrix"):
        matrix = pose.matrix()
        matrix = np.asarray(matrix, dtype=np.float64)
        if matrix.shape == (3, 4):
            R, t = matrix[:, :3], matrix[:, 3]
            return -R.T @ t
    rotation = pose.rotation.matrix()
    translation = np.asarray(pose.translation, dtype=np.float64)
    return -np.asarray(rotation, dtype=np.float64).T @ translation


def vggt_centers(cameras_file: Path) -> tuple[list[str], np.ndarray]:
    data = np.load(cameras_file)
    names = [str(value) for value in data["image_names"].tolist()]
    extrinsics = np.asarray(data["extrinsic"], dtype=np.float64)
    centers = []
    for ext in extrinsics:
        R = ext[:3, :3]
        t = ext[:3, 3]
        centers.append(-R.T @ t)
    return names, np.asarray(centers)


def estimate_similarity(source: np.ndarray, target: np.ndarray):
    if len(source) < 3:
        raise RuntimeError("Need at least three common camera centers for Sim(3) alignment.")
    mu_x = source.mean(axis=0)
    mu_y = target.mean(axis=0)
    X = source - mu_x
    Y = target - mu_y
    covariance = (Y.T @ X) / len(source)
    U, singular, Vt = np.linalg.svd(covariance)
    S = np.eye(3)
    if np.linalg.det(U @ Vt) < 0:
        S[-1, -1] = -1
    R = U @ S @ Vt
    variance = float(np.sum(X * X) / len(source))
    scale = float(np.sum(singular * np.diag(S)) / max(variance, 1e-12))
    t = mu_y - scale * (R @ mu_x)
    return scale, R, t


def transform_dense_ply(source_ply: Path, target_ply: Path, scale: float, R: np.ndarray, t: np.ndarray):
    ply = PlyData.read(source_ply)
    vertex = ply["vertex"]
    xyz = np.stack([vertex["x"], vertex["y"], vertex["z"]], axis=1).astype(np.float64)
    transformed = (scale * (R @ xyz.T)).T + t
    colors = np.stack([vertex["red"], vertex["green"], vertex["blue"]], axis=1).astype(np.uint8)
    names = set(vertex.data.dtype.names or ())
    if {"nx", "ny", "nz"}.issubset(names):
        normals = np.stack([vertex["nx"], vertex["ny"], vertex["nz"]], axis=1).astype(np.float64)
        normals = (R @ normals.T).T
        lengths = np.linalg.norm(normals, axis=1, keepdims=True)
        normals = np.divide(normals, np.maximum(lengths, 1e-12), out=np.zeros_like(normals), where=lengths > 1e-12)
    else:
        normals = np.zeros_like(transformed, dtype=np.float64)

    dtype = [
        ("x", "f4"), ("y", "f4"), ("z", "f4"),
        ("nx", "f4"), ("ny", "f4"), ("nz", "f4"),
        ("red", "u1"), ("green", "u1"), ("blue", "u1"),
    ]
    output = np.empty(len(transformed), dtype=dtype)
    attrs = np.concatenate(
        [transformed.astype(np.float32), normals.astype(np.float32), colors.astype(np.uint8)], axis=1
    )
    output[:] = list(map(tuple, attrs))
    PlyData([PlyElement.describe(output, "vertex")], text=False).write(target_ply)
    return len(transformed)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", type=Path, required=True)
    parser.add_argument("--vggt", type=Path, required=True)
    parser.add_argument("--sfm", type=Path, required=True)
    parser.add_argument("--geometry", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    if args.output.exists():
        if not args.overwrite:
            raise RuntimeError(f"Output exists: {args.output}")
        shutil.rmtree(args.output)
    images_out = args.output / "images"
    depths_out = args.output / "depths"
    sparse_out = args.output / "sparse/0"
    images_out.mkdir(parents=True)
    depths_out.mkdir(parents=True)
    sparse_out.mkdir(parents=True)

    images_in = args.frames / "images"
    depths_in = args.vggt / "depths"
    for image in sorted(images_in.iterdir()):
        if image.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            continue
        shutil.copy2(image, images_out / image.name)
        depth = depths_in / f"{image.stem}.png"
        if not depth.is_file():
            raise RuntimeError(f"Missing VGGT depth prior for {image.name}: {depth}")
        shutil.copy2(depth, depths_out / depth.name)

    for name in ["cameras.bin", "images.bin", "points3D.bin", "frames.bin", "rigs.bin"]:
        source = args.sfm / name
        if source.is_file():
            shutil.copy2(source, sparse_out / name)
    for required in ["cameras.bin", "images.bin", "points3D.bin"]:
        if not (sparse_out / required).is_file():
            raise RuntimeError(f"Missing COLMAP model file: {args.sfm / required}")

    reconstruction = pycolmap.Reconstruction(str(args.sfm))
    vggt_names, omega_centers = vggt_centers(args.vggt / "cameras.npz")
    omega_by_name = {name: center for name, center in zip(vggt_names, omega_centers)}
    colmap_by_name = {
        image.name: projection_center(image)
        for image in reconstruction.images.values()
    }
    common = sorted(set(omega_by_name) & set(colmap_by_name))
    geometry_source = args.vggt / "pointcloud.ply"
    geometry_kind = "raw_vggt"
    if args.geometry and (args.geometry / "surface.ply").is_file():
        geometry_source = args.geometry / "surface.ply"
        geometry_kind = "cross_view_fused_surface"

    alignment_report = {
        "common_cameras": len(common),
        "used_dense_vggt_initialization": False,
        "geometry_source": geometry_kind,
        "geometry_file": str(geometry_source),
    }

    if len(common) >= 3 and geometry_source.is_file():
        source = np.stack([omega_by_name[name] for name in common])
        target = np.stack([colmap_by_name[name] for name in common])
        scale, rotation, translation = estimate_similarity(source, target)
        aligned_centers = (scale * (rotation @ source.T)).T + translation
        rms = float(np.sqrt(np.mean(np.sum((aligned_centers - target) ** 2, axis=1))))
        point_count = transform_dense_ply(
            geometry_source,
            sparse_out / "points3D.ply",
            scale,
            rotation,
            translation,
        )
        alignment_report.update(
            {
                "used_dense_vggt_initialization": True,
                "scale": scale,
                "rotation": rotation.tolist(),
                "translation": translation.tolist(),
                "camera_center_rms_after_alignment": rms,
                "dense_points": point_count,
            }
        )
        print(
            f"Aligned {geometry_kind} geometry to COLMAP with {len(common)} cameras; "
            f"RMS={rms:.6f}"
        )
    else:
        print("[WARN] Could not align VGGT dense point cloud; Graphdeco will initialize from COLMAP sparse points.")

    (args.output / "vggt_to_colmap_alignment.json").write_text(
        json.dumps(alignment_report, indent=2)
    )

    cmd = [
        "python",
        "/opt/gaussian-splatting/utils/make_depth_scale.py",
        "--base_dir",
        str(args.output),
        "--depths_dir",
        str(depths_out),
        "--model_type",
        "bin",
    ]
    print("Running Graphdeco inverse-depth scale calibration...")
    subprocess.run(cmd, cwd="/opt/gaussian-splatting/utils", check=True)

    params_file = sparse_out / "depth_params.json"
    params = json.loads(params_file.read_text())
    valid = [value for value in params.values() if float(value.get("scale", 0)) > 0]
    summary = {
        "images": len(list(images_out.glob("*"))),
        "registered_images": int(reconstruction.num_reg_images()),
        "sfm_points": int(reconstruction.num_points3D()),
        "depth_maps": len(list(depths_out.glob("*.png"))),
        "depth_scales_valid": len(valid),
        "dense_initialization": alignment_report,
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    print("=== DEPTH-REGULARIZED GS DATASET COMPLETE ===")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
