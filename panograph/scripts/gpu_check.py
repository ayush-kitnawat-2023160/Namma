#!/usr/bin/env python3
from __future__ import annotations

import json

import torch

from common import env_float, env_int, load_config


def main() -> None:
    cfg = load_config()["panovggt"]
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is not available inside the container.")
    free, total = torch.cuda.mem_get_info(0)
    free_mb = free // (1024 * 1024)
    total_mb = total // (1024 * 1024)
    minimum = env_int("NP_GPU_MIN_FREE_MB", int(cfg["min_free_vram_mb"]))
    fraction = env_float("NP_GPU_MEMORY_FRACTION", float(cfg["gpu_memory_fraction"]))
    info = {
        "device": torch.cuda.get_device_name(0),
        "free_vram_mb": int(free_mb),
        "total_vram_mb": int(total_mb),
        "required_free_vram_mb": minimum,
        "torch_process_memory_fraction": fraction,
    }
    print(json.dumps(info, indent=2))
    if free_mb < minimum:
        raise RuntimeError(
            f"Refusing GPU inference: only {free_mb} MiB free; need >= {minimum} MiB. "
            "This protects the shared server from memory pressure."
        )


if __name__ == "__main__":
    main()
