#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

import torch


GIB = 1024 ** 3


def gpu_temperature() -> int | None:
    try:
        value = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=temperature.gpu",
                "--format=csv,noheader,nounits",
                "-i",
                "0",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip().splitlines()[0]
        return int(value)
    except Exception:
        return None


def cgroup_memory_limit() -> str:
    for path in (Path("/sys/fs/cgroup/memory.max"), Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")):
        if not path.is_file():
            continue
        value = path.read_text().strip()
        if value == "max":
            return "unlimited"
        try:
            return f"{int(value) / GIB:.1f} GiB"
        except ValueError:
            pass
    return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description="Fail-safe preflight for Namma GPU stages")
    parser.add_argument("--stage", default="gpu-stage")
    parser.add_argument("--min-free-gb", type=float, default=float(os.getenv("NG_MIN_FREE_VRAM_GB", "18")))
    parser.add_argument("--max-temp-c", type=int, default=int(os.getenv("NG_MAX_GPU_TEMP_C", "82")))
    args = parser.parse_args()

    if not torch.cuda.is_available():
        raise SystemExit(f"[GPU GUARD] {args.stage}: CUDA is not visible; refusing to start heavy GPU work.")

    props = torch.cuda.get_device_properties(0)
    free, total = torch.cuda.mem_get_info(0)
    free_gb = free / GIB
    total_gb = total / GIB
    temp = gpu_temperature()

    print(f"[GPU GUARD] stage={args.stage}")
    print(f"[GPU GUARD] gpu={props.name} free={free_gb:.2f}/{total_gb:.2f} GiB")
    print(f"[GPU GUARD] container_memory_limit={cgroup_memory_limit()}")
    if temp is not None:
        print(f"[GPU GUARD] temperature={temp} C (limit {args.max_temp_c} C)")

    if free_gb < args.min_free_gb:
        raise SystemExit(
            f"[GPU GUARD] Refusing {args.stage}: only {free_gb:.2f} GiB VRAM is free; "
            f"need at least {args.min_free_gb:.2f} GiB. Stop other GPU jobs or lower the workload."
        )
    if temp is not None and temp >= args.max_temp_c:
        raise SystemExit(
            f"[GPU GUARD] Refusing {args.stage}: GPU is already {temp} C, at/above the "
            f"{args.max_temp_c} C safety limit."
        )

    print("[GPU GUARD] safe to start")


if __name__ == "__main__":
    main()
