#!/usr/bin/env python3
"""Adapt the pinned Graphdeco trainer without changing the baseline installation."""
from __future__ import annotations

import argparse
import math
import runpy
import sys
from pathlib import Path


def growth_slots(current: int, maximum: int) -> int:
    # A split adds two children before deleting its parent. Reserve for that
    # transient peak as well as the final count, rather than checking afterward.
    return max(0, (maximum - current) // 2)


def install_adapters(maximum: int) -> None:
    import cv2
    import numpy as np
    import torch
    import scene.cameras as cameras
    import utils.camera_utils as camera_utils
    from scene.gaussian_model import GaussianModel

    original_camera = cameras.Camera

    class StereoCamera(original_camera):
        def __init__(self, *args, **kwargs):
            raw_depth = kwargs.get('invdepthmap', args[8] if len(args) > 8 else None)
            super().__init__(*args, **kwargs)
            if self.invdepthmap is not None:
                # Graphdeco's default mask is all ones, including missing depth.
                # Erode valid samples to avoid bilinear interpolation across holes.
                if raw_depth is None:
                    raise RuntimeError('Pinned Camera constructor changed: missing raw stereo depth.')
                raw_valid = (np.isfinite(raw_depth) & (raw_depth > 0)).astype(np.uint8)
                valid = cv2.resize(raw_valid, (self.image_width, self.image_height), interpolation=cv2.INTER_NEAREST)
                valid = cv2.erode(valid, np.ones((3, 3), np.uint8), borderType=cv2.BORDER_CONSTANT, borderValue=0)
                self.depth_mask *= torch.from_numpy(valid[None]).to(self.data_device)
                self.depth_mask *= self.alpha_mask
                self.depth_reliable = self.depth_reliable and bool(valid.any())

    cameras.Camera = StereoCamera
    camera_utils.Camera = StereoCamera
    original_densify = GaussianModel.densify_and_prune

    def bounded_densify(self, max_grad, min_opacity, extent, max_screen_size, radii):
        current = len(self.get_xyz)
        if current > maximum:
            raise RuntimeError(f'Gaussian budget exceeded: {current}>{maximum}')
        slots = growth_slots(current, maximum)
        grads = torch.nan_to_num(self.xyz_gradient_accum / self.denom, nan=0.0, posinf=0.0, neginf=0.0)
        norms = torch.linalg.vector_norm(grads, dim=-1)
        threshold = float(max_grad)
        eligible = norms[norms >= threshold]
        if slots == 0:
            threshold = math.inf
        elif len(eligible) > slots:
            cutoff = torch.topk(eligible, slots).values[-1]
            # Strictly exclude ties at the budget boundary; never allocate a
            # large tied group simply because gradients share the same value.
            threshold = max(threshold, float(torch.nextafter(cutoff, torch.full_like(cutoff, math.inf))))
        # Match the sanitized gradients used to choose the bound.
        self.xyz_gradient_accum.copy_(grads * self.denom.nan_to_num())
        original_densify(self, threshold, min_opacity, extent, max_screen_size, radii)
        if len(self.get_xyz) > maximum:
            raise RuntimeError('Pinned Graphdeco densification contract changed; refusing further training.')

    GaussianModel.densify_and_prune = bounded_densify


def main() -> None:
    import torch

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--max-gaussians', type=int, default=1_200_000)
    parser.add_argument('--fraction', type=float, default=0.85)
    parser.add_argument('script', type=Path)
    args, remaining = parser.parse_known_args()
    if not 100_000 <= args.max_gaussians <= 1_500_000:
        raise SystemExit('Gaussian cap must be 100k..1.5M.')
    if not 0.5 <= args.fraction <= 0.88:
        raise SystemExit('Allocator fraction must be 0.5..0.88 for this profile.')
    if not torch.cuda.is_available():
        raise SystemExit('CUDA is required on the college server.')
    torch.cuda.set_per_process_memory_fraction(args.fraction, 0)
    script = args.script.resolve()
    if script.parent != Path('/opt/gaussian-splatting') or script.name not in {'train.py', 'render.py'}:
        raise SystemExit('Only the pinned Graphdeco train/render entrypoints are supported.')
    sys.path.insert(0, str(script.parent))
    install_adapters(args.max_gaussians)
    sys.argv = [str(script), *remaining]
    runpy.run_path(str(script), run_name='__main__')


if __name__ == '__main__':
    main()
