#!/usr/bin/env python3
"""Call existing frame preparation with a bounded calibration subprocess."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def main() -> None:
    # Run calibration in a capped process, then call the frame extractor
    # directly. The legacy shell accepts only a camera profile basename.
    video, scene = sys.argv[1:]
    source = Path(video)
    if not source.is_file():
        source = Path('/workspace/data') / video
    source = source.resolve(strict=True)
    if not source.is_relative_to(Path('/workspace/data')):
        raise SystemExit('Video must be located under the read-only data/ mount.')
    env = os.environ.copy()
    legacy = Path('/opt/namma-nextgen/scripts')
    profile_name = env.get('NG_CAMERA_PROFILE')
    if not profile_name:
        output = Path('/workspace/outputs/nextgen') / scene / '00_calibration'
        output.mkdir(parents=True)
        subprocess.run([sys.executable, str(legacy / 'gpu_guard.py'), '--stage', 'precision-calibration'], check=True)
        subprocess.run([sys.executable, str(legacy / 'torch_limited_exec.py'),
            '--fraction', env.get('NG_GPU_MEMORY_FRACTION', '0.85'), str(legacy / 'calibrate_video.py'),
            '--video', str(source), '--output', str(output / 'camera.yaml'),
            '--report', str(output / 'report.json'), '--views', env.get('NG_CALIB_VIEWS', '20'),
            '--camera-model', env.get('NG_CALIB_MODEL', 'radial')], check=True)
        profile = output / 'camera.yaml'
    else:
        if not all(char.isalnum() or char in '_-' for char in profile_name):
            raise SystemExit('NG_CAMERA_PROFILE must be a camera profile basename.')
        profile = Path('/opt/namma/config/cameras') / (profile_name + '.yaml')
        if not profile.is_file():
            raise SystemExit(f'Camera profile missing: {profile}')
    subprocess.run([sys.executable, str(legacy / 'prepare_scene.py'),
        '--video', str(source), '--output', str(Path('/workspace/outputs/nextgen') / scene / '01_frames'),
        '--camera-profile', str(profile), '--target-views', env.get('NG_TARGET_VIEWS', '160'),
        '--max-candidates', env.get('NG_MAX_CANDIDATES', '1200'),
        '--min-blur', env.get('NG_MIN_BLUR', '20'),
        '--duplicate-distance', env.get('NG_DUPLICATE_DISTANCE', '0.025')], env=env, check=True)


if __name__ == '__main__':
    main()
