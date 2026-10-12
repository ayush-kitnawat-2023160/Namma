#!/usr/bin/env python3
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import torch


def check(module: str) -> None:
    try:
        imported = importlib.import_module(module)
        version = getattr(imported, "__version__", "")
        print(f"[ OK ] {module} {version}")
    except Exception as exc:
        print(f"[FAIL] {module}: {exc}", file=sys.stderr)
        raise SystemExit(1)


print("=== Namma NextGen preflight ===")
print("Python:", sys.version.split()[0])
print("PyTorch:", torch.__version__, "CUDA build:", torch.version.cuda)

for name in [
    "cv2",
    "numpy",
    "yaml",
    "pycolmap",
    "hloc",
    "lightglue",
    "geocalib",
    "vggt_omega",
    "plyfile",
]:
    check(name)

if torch.cuda.is_available():
    p = torch.cuda.get_device_properties(0)
    free, total = torch.cuda.mem_get_info()
    print(f"[ OK ] GPU: {p.name} compute {p.major}.{p.minor}")
    print(f"[ OK ] VRAM free/total: {free/1024**3:.1f}/{total/1024**3:.1f} GiB")
else:
    print("[WARN] CUDA not visible. CPU-only commands can still run.")

checkpoint = Path(
    __import__("os").environ.get(
        "NG_VGGT_CHECKPOINT",
        "/workspace/checkpoints/VGGT-Omega-1B-512.pt",
    )
)
if checkpoint.is_file():
    print(f"[ OK ] VGGT-Omega checkpoint: {checkpoint}")
else:
    print(f"[WARN] VGGT-Omega checkpoint not found: {checkpoint}")

print("=== NextGen preflight complete ===")
