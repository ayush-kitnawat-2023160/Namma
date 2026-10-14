#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path("/workspace/semantic")
STAGES = [
    ("00_source", "source manifest"),
    ("01_segmentation", "2D masks"),
    ("02_semantic_cloud", "3D semantic fusion"),
    ("03_layout", "room shell / planes"),
    ("04_object_meshes", "per-object meshes"),
    ("05_textures", "texture baking"),
    ("06_scene", "GLB/USD scene"),
    ("07_viewer", "first-person package"),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene", required=True)
    args = parser.parse_args()
    base = ROOT / args.scene
    print(f"Semantic scene: {args.scene}")
    for directory, description in STAGES:
        path = base / directory
        marker = "READY" if path.exists() and any(path.iterdir()) else "pending"
        print(f"[{marker:7}] {directory:20} {description}")


if __name__ == "__main__":
    main()

