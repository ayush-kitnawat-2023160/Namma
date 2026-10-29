#!/usr/bin/env python3
"""Stream held-out render pairs; no metric network/checkpoint downloads."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--iteration', type=int, required=True)
    args = parser.parse_args()
    import numpy as np
    from PIL import Image
    import torch

    sys.path.insert(0, '/opt/gaussian-splatting')
    from utils.loss_utils import ssim

    method = f'ours_{args.iteration}'
    folder = args.model / 'test' / method
    names = sorted(p.name for p in (folder / 'renders').glob('*.png'))
    if not names or names != sorted(p.name for p in (folder / 'gt').glob('*.png')):
        raise RuntimeError('Missing/mismatched held-out render and ground-truth images.')
    # CPU evaluation avoids another GPU memory peak and uses the same pinned
    # Graphdeco SSIM definition. Only one RGB pair is resident at a time.
    torch.set_num_threads(4)
    per_view = {}
    with torch.no_grad():
        for name in names:
            tensors = []
            for subdir in ['renders', 'gt']:
                with Image.open(folder / subdir / name) as image:
                    array = np.asarray(image.convert('RGB'), dtype=np.float32) / 255.0
                tensors.append(torch.from_numpy(array.copy()).permute(2, 0, 1).unsqueeze(0))
            render, reference = tensors
            if render.shape != reference.shape:
                raise RuntimeError(f'Render/GT size mismatch: {name}')
            mse = float(torch.mean((render - reference) ** 2))
            # The floor gives a finite 120dB for an exactly matching pair.
            per_view[name] = {'PSNR': float(-10 * np.log10(max(mse, 1e-12))),
                              'SSIM': float(ssim(render, reference))}
    result = {method: {key: float(np.mean([v[key] for v in per_view.values()]))
                       for key in ['PSNR', 'SSIM']}}
    (args.model / 'results.json').write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    (args.model / 'per_view.json').write_text(json.dumps({method: per_view}, indent=2, allow_nan=False) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
