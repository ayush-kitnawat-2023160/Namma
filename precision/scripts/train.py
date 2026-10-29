#!/usr/bin/env python3
"""Train on supported stereo depth and evaluate withheld video frames."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scene-root', type=Path, required=True)
    args = parser.parse_args()
    root = args.scene_root
    dataset, model = root / '05_gs_dataset', root / '06_gs_model'
    if model.exists():
        raise SystemExit('Refusing to overwrite trained/partial model.')
    summary = json.loads((dataset / 'summary.json').read_text())
    maximum = int(os.getenv('PR_MAX_GAUSSIANS', '1200000'))
    if not summary['initial_gaussians'] <= maximum <= 1_500_000:
        raise SystemExit('Gaussian cap must cover initial points and be <=1.5M.')
    iterations = int(os.getenv('NG_GS_ITERS', '30000'))
    model.mkdir()
    scripts = Path('/opt/namma-precision/scripts')
    gs = Path('/opt/gaussian-splatting')
    prefix = [sys.executable, str(scripts / 'safe_train.py'), '--max-gaussians', str(maximum),
              '--fraction', os.getenv('NG_GPU_MEMORY_FRACTION', '0.85')]
    command = [*prefix, str(gs / 'train.py'), '-s', str(dataset), '-m', str(model),
        '-d', 'depths', '--iterations', str(iterations), '--save_iterations', str(iterations),
        '--test_iterations', '7000', str(iterations), '--checkpoint_iterations', '7000', str(iterations),
        '--resolution', os.getenv('NG_GS_RESOLUTION', '1'), '--data_device', 'cpu',
        '--depth_l1_weight_init', '0.2', '--depth_l1_weight_final', '0.02',
        '--densification_interval', '200', '--densify_until_iter', '8000',
        '--densify_grad_threshold', '0.0003', '--eval', '--antialiasing', '--disable_viewer']
    print('Training with withheld frames (--eval), CPU cameras, bounded densification.', flush=True)
    subprocess.run(command, cwd=gs, check=True)
    # Keep renders/metrics in the Graphdeco model folder (standard tools layout).
    subprocess.run([*prefix, str(gs / 'render.py'), '-m', str(model), '--iteration', str(iterations),
                    '--skip_train', '--antialiasing'], cwd=gs, check=True)
    subprocess.run([sys.executable, str(scripts / 'evaluate.py'), '--model', str(model),
                    '--iteration', str(iterations)], cwd=gs, check=True)
    result = {'iterations': iterations, 'max_gaussians': maximum, 'camera_data_device': 'cpu',
              'heldout': 'every eighth selected video frame (Graphdeco --eval)',
              'metrics': str(model / 'results.json'), 'depth_source': 'geometric stereo; zero depth masked'}
    (model / 'precision_training.json').write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
