#!/usr/bin/env python3
from __future__ import annotations

import json
import torch

from common import env, env_float, env_int, load_config

cfg = load_config()['safety']
profile = env('PS_PROFILE', '512')
if not torch.cuda.is_available():
    raise RuntimeError('CUDA is required for PanSplat.')
free, total = torch.cuda.mem_get_info(0)
free_mb = int(free // 2**20)
total_mb = int(total // 2**20)
minimum = env_int('PS_GPU_MIN_FREE_MB', int(cfg[f'min_free_vram_mb_{profile}']))
fraction = env_float('PS_GPU_MEMORY_FRACTION', float(cfg[f'gpu_memory_fraction_{profile}']))
if free_mb < minimum:
    raise RuntimeError(f'GPU guard: {free_mb} MiB free < required {minimum} MiB for profile={profile}')
print(json.dumps({
    'device': torch.cuda.get_device_name(0),
    'profile': profile,
    'free_vram_mb': free_mb,
    'total_vram_mb': total_mb,
    'required_free_vram_mb': minimum,
    'torch_memory_fraction': fraction,
}, indent=2))
