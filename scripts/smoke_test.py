#!/usr/bin/env python3
"""CPU-safe image/integration smoke test; no real SLAM inference is attempted."""
from __future__ import annotations

import importlib
import struct
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


def run(cmd: list[str], cwd: str | None = None) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True)


print("=== Namma-IITB CPU-safe smoke test ===")
for p in [
    Path("/opt/MASt3R-SLAM/main.py"),
    Path("/opt/SplatSLAM/scripts/export_mast3r_to_3dgs.py"),
    Path("/opt/gaussian-splatting/train.py"),
]:
    assert p.is_file(), f"Missing {p}"
    print(f"[ OK ] {p}")

for module in [
    "mast3r_slam",
    "mast3r_slam_backends",
    "mast3r",
    "diff_gaussian_rasterization",
    "simple_knn",
    "fused_ssim",
    "open3d",
]:
    importlib.import_module(module)
    print(f"[ OK ] import {module}")

run(["python", "/opt/MASt3R-SLAM/main.py", "--help"], cwd="/opt/MASt3R-SLAM")
run(["python", "/opt/gaussian-splatting/train.py", "--help"], cwd="/opt/gaussian-splatting")

with tempfile.TemporaryDirectory(prefix="namma-smoke-") as td:
    root = Path(td)
    video = root / "tiny.avi"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 5.0, (64, 48))
    assert writer.isOpened()
    for i in range(5):
        writer.write(np.full((48, 64, 3), i * 40, np.uint8))
    writer.release()
    cap = cv2.VideoCapture(str(video))
    ok, frame = cap.read()
    cap.release()
    assert ok and frame.shape[:2] == (48, 64)
    print("[ OK ] OpenCV video decode")

    scene = "tiny"
    source = root / "mast3r" / scene
    kf = source / "keyframes" / scene
    kf.mkdir(parents=True)
    Image.new("RGB", (64, 48), (120, 80, 40)).save(kf / "0.0.png")
    Image.new("RGB", (64, 48), (80, 120, 40)).save(kf / "0.2.png")
    (source / f"{scene}.txt").write_text(
        "0.0 0 0 0 0 0 0 1\n"
        "0.2 0.1 0 0 0 0 0 1\n"
    )
    ply = source / f"{scene}.ply"
    pts = [
        (0.0, 0.0, 1.0, 255, 0, 0),
        (0.1, 0.0, 1.0, 0, 255, 0),
        (0.0, 0.1, 1.0, 0, 0, 255),
    ]
    with ply.open("wb") as f:
        f.write((
            "ply\nformat binary_little_endian 1.0\n"
            f"element vertex {len(pts)}\n"
            "property float x\nproperty float y\nproperty float z\n"
            "property uchar red\nproperty uchar green\nproperty uchar blue\n"
            "end_header\n"
        ).encode())
        for row in pts:
            f.write(struct.pack("<fffBBB", *row))

    converted = root / "converted"
    run([
        "python", "/opt/SplatSLAM/scripts/export_mast3r_to_3dgs.py",
        "--source", str(source), "--output", str(converted), "--scene", scene,
        "--max-points", "3", "--fov-x", "60",
    ], cwd="/opt/SplatSLAM")
    for rel in [
        "images/000001.png",
        "sparse/0/cameras.txt",
        "sparse/0/images.txt",
        "sparse/0/points3D.txt",
    ]:
        p = converted / rel
        assert p.is_file() and p.stat().st_size > 0, f"Missing {p}"
    print("[ OK ] synthetic MASt3R -> COLMAP/3DGS conversion")


    points_txt = converted / "sparse/0/points3D.txt"
    points_ply = converted / "sparse/0/points3D.ply"

    if not points_ply.is_file():
        print("[ INFO ] Exporter did not create points3D.ply")
        print("[ INFO ] Creating points3D.ply from points3D.txt")

        run([
            "python",
            "/opt/namma/scripts/points3d_txt_to_ply.py",
            str(points_txt),
            str(points_ply),
        ])

    assert points_ply.is_file(), f"Missing {points_ply}"
    assert points_ply.stat().st_size > 0

    print("[ OK ] points3D PLY generation")

print("=== Smoke test passed ===")

