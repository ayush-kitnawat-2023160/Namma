#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from common import scene_root


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("scene", nargs="?", default="hindoor_unibs")
    args = parser.parse_args()
    root = scene_root(args.scene)
    checks = [
        ("01_panoramas", root / "01_panoramas" / "manifest.json", "prepared 360 panoramas"),
        ("02_geometry", root / "02_geometry" / "summary.json", "pairwise PanoVGGT poses"),
        ("03_graph", root / "03_graph" / "graph.json", "PanoGraph nodes/edges"),
        ("04_sphr", root / "04_sphr" / "bootstrap.json", "SPHR viewer package"),
    ]
    print(f"PanoGraph scene: {args.scene}")
    for stage, path, description in checks:
        state = "READY" if path.is_file() else "-----"
        print(f"[{state:5}] {stage:16} {description}")


if __name__ == "__main__":
    main()
