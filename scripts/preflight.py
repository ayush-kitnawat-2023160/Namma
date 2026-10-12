#!/usr/bin/env python3
from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path

import torch


def fail(message: str) -> None:
    print(f"[FAIL] {message}", file=sys.stderr)
    raise SystemExit(1)


def ok(message: str) -> None:
    print(f"[ OK ] {message}")


print("=== Namma-IITB GPU preflight ===")
print(f"Python: {sys.version.split()[0]}")
print(f"PyTorch: {torch.__version__}")
print(f"Torch CUDA build: {torch.version.cuda}")

if not shutil.which("nvcc"):
    fail("nvcc missing inside image")
nvcc = subprocess.check_output(["nvcc", "--version"], text=True).strip().splitlines()[-1]
ok(f"nvcc: {nvcc}")

if not torch.cuda.is_available():
    fail("CUDA is not visible. Start via ./run.sh and verify NVIDIA Container Toolkit on the host.")

ok(f"CUDA visible: {torch.cuda.device_count()} device(s)")
for i in range(torch.cuda.device_count()):
    p = torch.cuda.get_device_properties(i)
    print(f"  GPU {i}: {p.name} | compute {p.major}.{p.minor} | {p.total_memory/1024**3:.1f} GiB")

p = torch.cuda.get_device_properties(0)
if p.total_memory / 1024**3 < 18:
    print("[WARN] Less than 18 GiB total VRAM; increase SUBSAMPLE and reduce MAX_POINTS.")

for module in [
    "mast3r_slam",
    "mast3r_slam_backends",
    "mast3r",
    "diff_gaussian_rasterization",
    "simple_knn",
    "fused_ssim",
    "open3d",
    "plyfile",
]:
    try:
        importlib.import_module(module)
        ok(f"import {module}")
    except Exception as exc:
        fail(f"cannot import {module}: {exc}")

ckpt = Path("/opt/MASt3R-SLAM/checkpoints")
for name in [
    "MASt3R_ViTLarge_BaseDecoder_512_catmlpdpt_metric.pth",
    "MASt3R_ViTLarge_BaseDecoder_512_catmlpdpt_metric_retrieval_trainingfree.pth",
    "MASt3R_ViTLarge_BaseDecoder_512_catmlpdpt_metric_retrieval_codebook.pkl",
]:
    pth = ckpt / name
    if not pth.is_file() or pth.stat().st_size == 0:
        fail(f"missing checkpoint: {pth}")
    ok(f"checkpoint {name} ({pth.stat().st_size/1024**2:.1f} MiB)")

x = torch.tensor([1.0, 2.0, 3.0], device="cuda")
if (x * 2).sum().item() != 12.0:
    fail("CUDA arithmetic test failed")
ok("CUDA arithmetic")

free, total = torch.cuda.mem_get_info()
print(f"GPU0 free/total: {free/1024**3:.1f}/{total/1024**3:.1f} GiB")
if free / 1024**3 < 16:
    print("[WARN] Less than 16 GiB is currently free. Another process may be occupying the GPU.")

print("=== Preflight passed ===")

