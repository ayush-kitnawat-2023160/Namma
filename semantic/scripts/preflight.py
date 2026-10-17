#!/usr/bin/env python3
from __future__ import annotations

import importlib
import sys


MODULES = [
    "numpy",
    "cv2",
    "yaml",
    "pycolmap",
    "open3d",
    "trimesh",
    "skimage",
    "sklearn",
    "networkx",
    "scipy",
    "plyfile",
    "xatlas",
    "transformers",
    "accelerate",
]


def main() -> None:
    print("Python", sys.version.split()[0])
    failed = []
    for name in MODULES:
        try:
            module = importlib.import_module(name)
            version = getattr(module, "__version__", "ok")
            print(f"{name}: {version}")
        except Exception as exc:  # pragma: no cover - diagnostic tool
            failed.append((name, str(exc)))
            print(f"{name}: FAILED ({exc})")
    if failed:
        raise SystemExit(1)
    print("Semantic geometry environment: OK")


if __name__ == "__main__":
    main()
