#!/usr/bin/env python3
"""Summarize persistent evidence without loading geometry or a GPU."""
import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scene-root', type=Path, required=True)
    root = parser.parse_args().scene_root
    if not root.is_dir():
        raise SystemExit(f'Scene missing: {root}')
    paths = ['run_manifest.json', '03_sfm/summary.json',
             '04_mvs/quality_report.json', '04_mvs/summary.json', '05_gs_dataset/summary.json',
             '06_gs_model/precision_training.json', '06_gs_model/results.json', '08_mesh/summary.json']
    report = {name: json.loads((root / name).read_text()) for name in paths if (root / name).is_file()}
    report['limitations'] = ['Unobserved surfaces remain unknown.',
        'Monocular geometry has arbitrary scale until a measured distance is supplied.',
        'Heldout image scores measure appearance, not 3D geometric ground truth.',
        'This experimental integration requires a real server validation run.']
    text = json.dumps(report, indent=2) + '\n'
    (root / 'precision_report.json').write_text(text)
    print(text)


if __name__ == '__main__':
    main()
