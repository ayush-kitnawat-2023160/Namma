#!/usr/bin/env python3
"""CPU-only image dependency check. Does not allocate CUDA memory."""
import importlib
import json
import subprocess


def main() -> None:
    modules = {}
    for name in ['torch', 'cv2', 'numpy', 'pycolmap', 'open3d', 'plyfile', 'hloc', 'vggt_omega', 'geocalib']:
        module = importlib.import_module(name)
        modules[name] = getattr(module, '__version__', 'import OK')
    if modules['pycolmap'] != '3.11.1':
        raise SystemExit('Expected pycolmap 3.11.1 matching the pinned COLMAP CLI.')
    for command, token in [('patch_match_stereo', 'PatchMatchStereo.geom_consistency'),
                           ('stereo_fusion', 'StereoFusion.use_cache')]:
        result = subprocess.run(['colmap', command, '-h'], capture_output=True, text=True, check=True)
        if token not in result.stdout + result.stderr:
            raise SystemExit(f'Missing COLMAP option: {token}')
    from sfm_compatibility import check_sfm_compatibility
    compatibility = check_sfm_compatibility()
    print(json.dumps({'modules': modules, 'CUDA_work_executed': False,
                      'sfm_compatibility': compatibility,
                      'note': 'GPU capacity and runtime telemetry checked separately by the host launcher.'}, indent=2))


if __name__ == '__main__':
    main()
