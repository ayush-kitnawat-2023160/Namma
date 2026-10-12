#!/usr/bin/env python3

import sys
from pathlib import Path

if len(sys.argv) != 3:
    print("Usage: points3d_txt_to_ply.py points3D.txt points3D.ply")
    sys.exit(2)

src = Path(sys.argv[1])
dst = Path(sys.argv[2])

if not src.is_file():
    raise FileNotFoundError(src)

points = []

with src.open() as f:
    for line in f:
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        p = line.split()

        if len(p) < 7:
            continue

        x, y, z = map(float, p[1:4])
        r, g, b = map(int, p[4:7])

        points.append((x, y, z, r, g, b))

if not points:
    raise RuntimeError(f"No valid points found in {src}")

dst.parent.mkdir(parents=True, exist_ok=True)

with dst.open("w") as f:
    f.write("ply\n")
    f.write("format ascii 1.0\n")
    f.write(f"element vertex {len(points)}\n")

    f.write("property float x\n")
    f.write("property float y\n")
    f.write("property float z\n")

    # Graphdeco 3DGS expects these fields.
    f.write("property float nx\n")
    f.write("property float ny\n")
    f.write("property float nz\n")

    f.write("property uchar red\n")
    f.write("property uchar green\n")
    f.write("property uchar blue\n")

    f.write("end_header\n")

    for x, y, z, r, g, b in points:
        # Zero normals are fine for 3DGS initialization.
        f.write(f"{x} {y} {z} 0 0 0 {r} {g} {b}\n")

print(f"[ OK ] Converted {len(points)} points")
print(f"[ OK ] Graphdeco-compatible PLY written to {dst}")
